#!/usr/bin/env python3
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

"""Give every component repository the same release labels.

Additive: a label the canonical set does not mention is left alone, so
repositories keep their own (`elixir`, `javascript`, `check`, ...). Only
labels named in release/labels.yaml are created or corrected.

Renames come first and matter. Renaming a label on GitHub keeps every
existing assignment, so `tech debt` becoming `tech-debt` carries its
pull requests with it; creating `tech-debt` alongside would leave them
uncategorised in the release notes forever.

    ./sync_labels.py                    # dry run, report the differences
    ./sync_labels.py --execute
    ./sync_labels.py --only web
"""

from __future__ import annotations

import argparse
import sys
import urllib.parse
from pathlib import Path

from common import COMPONENTS_FILE, LABELS_FILE, GitHub, load_config, load_yaml, log

SELF_REPO = ".github"


def canonical(path: Path) -> tuple[list[dict], list[dict]]:
    raw = load_yaml(path) or {}
    labels = [
        {
            "name": str(entry["name"]),
            # str() and not the parsed value: even quoted in the file, a
            # colour is only ever a six character hex string.
            "color": str(entry["color"]).lower(),
            "description": str(entry.get("description", "")),
        }
        for entry in raw.get("labels") or []
    ]
    renames = [dict(entry) for entry in raw.get("renames") or []]
    return labels, renames


class LabelSync(GitHub):
    def existing(self, repo: str) -> dict[str, dict]:
        return {
            entry["name"]: entry
            for entry in self.paginate(f"repos/{self.org}/{repo}/labels")
        }

    def create_label(self, repo: str, label: dict) -> None:
        self._write(
            f"create {repo}:{label['name']}",
            lambda: self._request(
                "POST",
                f"repos/{self.org}/{repo}/labels",
                {
                    "name": label["name"],
                    "color": label["color"],
                    "description": label.get("description", ""),
                },
            ),
        )

    def update_label(self, repo: str, current_name: str, label: dict) -> None:
        self._write(
            f"update {repo}:{current_name} -> {label['name']} #{label['color']}",
            lambda: self._request(
                "PATCH",
                f"repos/{self.org}/{repo}/labels/{urllib.parse.quote(current_name)}",
                {
                    "new_name": label["name"],
                    "color": label["color"],
                    "description": label.get("description", ""),
                },
            ),
        )


def sync_repo(client: LabelSync, repo: str, labels: list[dict], renames: list[dict]) -> list[str]:
    present = client.existing(repo)
    changes: list[str] = []

    for rename in renames:
        old, new = rename["from"], rename["to"]
        if old in present and new not in present:
            target = next(
                (label for label in labels if label["name"] == new),
                {"name": new, "color": "ededed", "description": ""},
            )
            changes.append(f"rename {old!r} -> {new!r}")
            client.update_label(repo, old, target)
            # The rename already carried the canonical colour and
            # description, so record it as correct and do not patch it
            # a second time below.
            present.pop(old)
            present[new] = dict(target)
        elif old in present and new in present:
            log(
                f"  ! {repo}: both {old!r} and {new!r} exist; merge them by hand, "
                "renaming would lose one"
            )

    for label in labels:
        current = present.get(label["name"])
        if current is None:
            changes.append(f"create {label['name']!r}")
            client.create_label(repo, label)
            continue
        if current.get("color", "").lower() != label["color"].lower() or (
            current.get("description") or ""
        ) != label.get("description", ""):
            changes.append(f"correct {label['name']!r}")
            client.update_label(repo, label["name"], label)

    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--components", type=Path, default=COMPONENTS_FILE)
    parser.add_argument("--labels", type=Path, default=LABELS_FILE)
    parser.add_argument("--only", action="append", default=[], help="restrict to these components")
    parser.add_argument("--execute", action="store_true", help="without it nothing is written")
    args = parser.parse_args(argv)

    config = load_config(args.components)
    labels, renames = canonical(args.labels)
    client = LabelSync(config.github_api, config.github_org, dry_run=not args.execute)

    selected = args.only or list(config.components)
    repos = [config.components[name].repo for name in config.components if name in selected]
    # The orchestration labels live on the manifest pull request here.
    if not args.only:
        repos.append(SELF_REPO)

    total = 0
    for repo in repos:
        log(f"{repo}")
        applicable = [rename for rename in renames if rename["repo"] in (repo, "*")]
        changes = sync_repo(client, repo, labels, applicable)
        total += len(changes)
        if not changes:
            log("  already in sync")

    print(f"{total} label change(s) {'applied' if args.execute else 'pending'}", file=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
