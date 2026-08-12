#!/usr/bin/env python3
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

"""Render the Trento release status tables.

Two tables, because one table with a column per SLES product is too wide
to read:

  Latest releases     GitHub, OBS rolling, OBS stable per component
  Availability        the version shipping in each SLES for SAP product

Both are derived entirely from collect_state.py output. No version is
written by hand anywhere.

    ./render_table.py --collect                     # markdown on stdout
    ./render_table.py --state state.json
    ./render_table.py --collect --inject profile/README.md
    ./render_table.py --collect --inject profile/README.md --check
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from common import REPO_ROOT, load_config, log

BEGIN_MARKER = "<!-- BEGIN trento-release-status -->"
END_MARKER = "<!-- END trento-release-status -->"
FOOTER_RE = re.compile(r"^<sub>Generated from .*$", re.MULTILINE)

GITHUB_REPO_URL = "https://github.com/{org}/{repo}"
OBS_PACKAGE_URL = "https://build.opensuse.org/package/show/{project}/{package}"


def collect(argv_extra: list[str]) -> dict:
    script = Path(__file__).with_name("collect_state.py")
    result = subprocess.run(
        [sys.executable, str(script), *argv_extra], capture_output=True, text=True, check=True
    )
    sys.stderr.write(result.stderr)
    return json.loads(result.stdout)


def em_dash_if_empty(value: str | None) -> str:
    return value if value else "—"


def obs_cell(version: str | None, project: str, package: str) -> str:
    """A version linked to the OBS package it was read from."""
    if not version:
        return "—"
    return f"[`{version}`]({OBS_PACKAGE_URL.format(project=project, package=package)})"


def render_releases_table(config, state: dict) -> str:
    stable_project = config.obs_projects.get("stable", "")
    rolling_project = config.obs_projects.get("rolling", "")

    lines = [
        f"| Project | Latest GitHub release | OBS `{stable_project}` | OBS `{rolling_project}` |",
        "| --- | --- | --- | --- |",
    ]
    for name in config.components:
        entry = state["components"][name]
        repo_url = GITHUB_REPO_URL.format(org=config.github_org, repo=entry["repo"])
        github = entry.get("github") or {}
        obs = entry.get("obs") or {}
        package = obs.get("package")

        version = github.get("version")
        release_cell = (
            f"[`{version}`]({github['url']})" if version and github.get("url") else em_dash_if_empty(version)
        )
        if package:
            stable_cell = obs_cell(obs.get("stable"), stable_project, package)
            rolling_cell = obs_cell(obs.get("rolling"), rolling_project, package)
        else:
            stable_cell = rolling_cell = "—"

        lines.append(f"| [{name}]({repo_url}) | {release_cell} | {stable_cell} | {rolling_cell} |")
    return "\n".join(lines)


def render_availability_table(config, state: dict) -> str:
    products = state.get("products") or []
    if not products:
        return "_No SLES availability data._"

    headers = [str(product.get("label", product["identifier"])) for product in products]
    lines = [
        "| Package | " + " | ".join(headers) + " |",
        "| --- | " + " | ".join("---" for _ in headers) + " |",
    ]
    for name in config.components:
        entry = state["components"][name]
        package = entry.get("obs_package")
        if not package:
            continue
        cells = []
        for product in products:
            available = (entry.get("scc") or {}).get(product["identifier"])
            cells.append(f"`{available['version']}`" if available else "—")
        if all(cell == "—" for cell in cells):
            continue
        lines.append(f"| `{package}` | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render(config, state: dict) -> str:
    modules = sorted(
        {
            module
            for entry in state["components"].values()
            for available in (entry.get("scc") or {}).values()
            for module in available.get("modules", [])
        }
    )
    module_note = ""
    if modules:
        families = sorted({module.split("/")[0] for module in modules})
        module_note = (
            "\nPackages reach these products through the "
            + ", ".join(f"`{family}`" for family in families)
            + " channels.\n"
        )

    return "\n".join(
        [
            BEGIN_MARKER,
            "",
            "## Release status",
            "",
            "### Latest releases",
            "",
            render_releases_table(config, state),
            "",
            "### Available in SUSE Linux Enterprise Server for SAP applications",
            "",
            render_availability_table(config, state),
            module_note.rstrip("\n"),
            "",
            f"<sub>Generated from the GitHub, OBS and SCC APIs on {state['generated_at']}. "
            "Do not edit by hand.</sub>",
            "",
            END_MARKER,
        ]
    )


def without_timestamp(block: str) -> str:
    return FOOTER_RE.sub("", block)


def inject(readme: Path, block: str) -> tuple[str, bool]:
    original = readme.read_text(encoding="utf-8") if readme.exists() else ""
    if BEGIN_MARKER in original and END_MARKER in original:
        start = original.index(BEGIN_MARKER)
        end = original.index(END_MARKER) + len(END_MARKER)
        # A refresh that found nothing new is not a change. Comparing
        # the whole block would make every daily run commit a new
        # timestamp, and would make --check fail on every pull request.
        if without_timestamp(original[start:end]) == without_timestamp(block):
            return original, False
        updated = original[:start] + block + original[end:]
    else:
        separator = "" if original.endswith("\n\n") or not original else "\n"
        updated = f"{original}{separator}\n{block}\n"
    return updated, updated != original


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--collect", action="store_true", help="collect fresh state")
    source.add_argument("--state", type=Path, help="read a state.json produced earlier")
    parser.add_argument("--inject", type=Path, help="README to update in place")
    parser.add_argument(
        "--check",
        action="store_true",
        help="with --inject, do not write; exit 1 if the file would change",
    )
    parser.add_argument("--skip-scc", action="store_true", help="passed through to collect_state.py")
    args = parser.parse_args(argv)

    config = load_config()
    state = collect(["--skip-scc"] if args.skip_scc else []) if args.collect else json.loads(
        args.state.read_text(encoding="utf-8")
    )
    block = render(config, state)

    if not args.inject:
        print(block)
        return 0

    target = args.inject if args.inject.is_absolute() else REPO_ROOT / args.inject
    updated, changed = inject(target, block)
    if args.check:
        log(f"{target}: {'would change' if changed else 'up to date'}")
        return 1 if changed else 0
    if changed:
        target.write_text(updated, encoding="utf-8")
        log(f"updated {target}")
    else:
        log(f"{target} already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
