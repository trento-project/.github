#!/usr/bin/env python3
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

"""Check components.yaml against the Actions variables it restates.

Two facts about a component live in two places. `obs_package` is written
here and, as the repository variable OBS_PACKAGE, in the component's own
repository, where obs-sync.yaml reads it. Whether a component goes to
OBS at all is `obs_package` being set here and OBS_ENABLED there.

Nothing forces them to agree, and a disagreement is quiet: the status
table would report an OBS package that nothing publishes to, and would
go on reporting it as "not submitted" forever. This says so instead.

Reading repository variables needs a token with administration read on
the component repositories. Without one there is nothing to compare, so
the check reports that and passes rather than failing a fork.

    ./check_variables.py
    ./check_variables.py --only web
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from common import COMPONENTS_FILE, Component, Config, GitHub, HttpError, load_config, log


class Variables(GitHub):
    def of(self, repo: str) -> dict[str, str] | None:
        """The repository's Actions variables, or None if unreadable.

        Not paginate(): this endpoint wraps the list in an object, and a
        component with more than a hundred variables is not a thing.
        """
        try:
            payload = self.get(f"repos/{self.org}/{repo}/actions/variables?per_page=100")
        except HttpError as error:
            if error.status in (401, 403, 404):
                return None
            raise
        return {entry["name"]: entry["value"] for entry in (payload or {}).get("variables", [])}


def problems_for(component: Component, variables: dict[str, str]) -> list[str]:
    found = variables.get("OBS_PACKAGE")
    enabled = (variables.get("OBS_ENABLED") or "").strip().lower() == "true"

    problems = []
    if component.obs_package and found and component.obs_package != found:
        problems.append(f"obs_package is {component.obs_package!r}, OBS_PACKAGE is {found!r}")
    elif component.obs_package and not found:
        problems.append(f"obs_package is {component.obs_package!r}, OBS_PACKAGE is unset")
    elif found and not component.obs_package:
        problems.append(f"OBS_PACKAGE is {found!r}, obs_package is unset")

    if component.obs_package and not enabled:
        problems.append("obs_package is set but OBS_ENABLED is not true, so nothing is submitted")

    return problems


def check_projects(config: Config) -> list[str]:
    """Report which OBS projects the run resolved, and from where.

    Organisation variables are not readable over the API without
    admin:org, so this cannot compare them. What it can do is name the
    values in force, which is what makes a wrong table explainable.
    """
    return [f"{name}: {project}" for name, project in sorted(config.obs_projects.items())]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--components", type=Path, default=COMPONENTS_FILE)
    parser.add_argument("--only", action="append", default=[], help="restrict to these components")
    args = parser.parse_args(argv)

    config = load_config(args.components)
    client = Variables(config.github_api, config.github_org, dry_run=True)

    log("OBS projects in force")
    for line in check_projects(config):
        log(f"  {line}")
    log("")

    selected = args.only or list(config.components)
    problems: list[str] = []
    unreadable: list[str] = []

    for name in config.components:
        if name not in selected:
            continue
        component = config.component(name)
        variables = client.of(component.repo)
        if variables is None:
            unreadable.append(component.repo)
            continue
        found = problems_for(component, variables)
        if not found:
            log(f"{name}: agrees with its repository variables")
            continue
        for problem in found:
            log(f"  ! {name}: {problem}")
        problems.extend(found)

    if unreadable:
        # A token that cannot read variables is the normal case on a
        # fork and on a pull request from one. Nothing was compared, so
        # nothing is claimed.
        print(
            f"could not read the variables of {', '.join(unreadable)}; "
            "needs a token with administration read",
            file=sys.stdout,
        )
    print(
        f"{len(problems)} disagreement(s) between components.yaml and the repository variables",
        file=sys.stdout,
    )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
