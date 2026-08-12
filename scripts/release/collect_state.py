#!/usr/bin/env python3
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileContributor: Generated with AI assistance

"""Collect the released state of every Trento component.

Three public, unauthenticated sources:

  GitHub  the latest published release per repository
  OBS     the version currently sitting in devel:sap:trento and
          devel:sap:trento:factory
  SCC     which SLES for SAP products ship which version

Writes a JSON document that render_table.py turns into the table in the
organisation profile. Read-only; safe to run at any time.

    ./collect_state.py                 # JSON on stdout
    ./collect_state.py -o state.json
    ./collect_state.py --skip-scc      # fast iteration, SCC is the slow one
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

from common import (
    COMPONENTS_FILE,
    Config,
    GitHub,
    Version,
    load_config,
    log,
    obs_package_version,
    product_label,
    product_release,
    scc_packages_for_product,
    scc_products,
)


def collect_github(config: Config, github: GitHub) -> dict:
    result: dict = {}
    for name, component in config.components.items():
        release = github.latest_release(component.repo)
        if release is None:
            log(f"  ! {name}: no published release")
            result[name] = {}
            continue
        result[name] = {
            "version": release["tag_name"],
            "published_at": release.get("published_at"),
            "url": release.get("html_url"),
        }
        log(f"  {name}: {release['tag_name']}")
    return result


def collect_obs(config: Config) -> dict:
    result: dict = {}
    for name, component in config.components.items():
        if not component.obs_package:
            result[name] = {}
            continue
        versions = {}
        for stream, project in config.obs_projects.items():
            versions[stream] = obs_package_version(
                config, project, component.obs_package, component.obs_version_file
            )
        result[name] = {"package": component.obs_package, **versions}
        log(f"  {name}: " + ", ".join(f"{k}={v}" for k, v in versions.items()))
    return result


def select_products(config: Config, products: list[dict], shipping: set[int]) -> list[dict]:
    """Keep only products that actually ship Trento, newest per major."""
    by_major: dict[int, list[dict]] = defaultdict(list)
    for product in products:
        if product["id"] not in shipping:
            continue
        by_major[product_release(product)[0]].append(product)

    selected: list[dict] = []
    for major in sorted(by_major, reverse=True):
        for product in by_major[major][: config.scc_max_products_per_major]:
            selected.append(
                {
                    "id": product["id"],
                    "identifier": product["identifier"],
                    "label": product_label(product),
                    "release": list(product_release(product)),
                }
            )
    return selected


def collect_scc(config: Config) -> tuple[list[dict], dict]:
    products = scc_products(config)
    log(f"  {len(products)} candidate products")

    package_owner: dict[str, str] = {}
    for name, component in config.components.items():
        for package in component.scc_packages:
            package_owner[package] = name

    # component -> product identifier -> {version, module}
    availability: dict[str, dict[str, dict]] = defaultdict(dict)
    shipping: set[int] = set()

    for product in products:
        entries = scc_packages_for_product(config, product["id"])
        found = False
        for entry in entries:
            component_name = package_owner.get(entry.get("name", ""))
            if component_name is None:
                continue
            found = True
            candidate = Version.parse_or_none(entry.get("version", "").split("+")[0])
            if candidate is None:
                continue
            current = availability[component_name].get(product["identifier"])
            if current is None or candidate > Version.parse(current["version"]):
                modules = sorted({p.get("identifier", "") for p in entry.get("products", [])})
                availability[component_name][product["identifier"]] = {
                    "version": str(candidate),
                    "release": entry.get("release"),
                    "modules": modules,
                }
        if found:
            shipping.add(product["id"])
        log(f"  {product['identifier']}: {'ships trento' if found else 'nothing'}")

    return select_products(config, products, shipping), dict(availability)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output", default="-", help="output file, or - for stdout")
    parser.add_argument("--components", type=Path, default=COMPONENTS_FILE)
    parser.add_argument("--skip-github", action="store_true")
    parser.add_argument("--skip-obs", action="store_true")
    parser.add_argument("--skip-scc", action="store_true")
    args = parser.parse_args(argv)

    config = load_config(args.components)
    github = GitHub(config.github_api, config.github_org)

    state: dict = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "products": [],
        "components": {},
    }

    github_state: dict = {}
    if not args.skip_github:
        log("GitHub releases")
        github_state = collect_github(config, github)

    obs_state: dict = {}
    if not args.skip_obs:
        log("OBS packages")
        obs_state = collect_obs(config)

    availability: dict = {}
    if not args.skip_scc:
        log("SCC availability")
        state["products"], availability = collect_scc(config)

    for name, component in config.components.items():
        state["components"][name] = {
            "repo": component.repo,
            "obs_package": component.obs_package,
            "github": github_state.get(name, {}),
            "obs": obs_state.get(name, {}),
            "scc": availability.get(name, {}),
        }

    payload = json.dumps(state, indent=2, sort_keys=False) + "\n"
    if args.output == "-":
        sys.stdout.write(payload)
    else:
        Path(args.output).write_text(payload, encoding="utf-8")
        log(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
