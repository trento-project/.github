#!/usr/bin/env python3
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

"""Turn release/manifest.yaml into an executable release plan.

The manifest says *what* to release. This says *how*: which repository
gets which branch, which files change, and in what order. It reads
GitHub and writes nothing, so it is safe to run on a pull request from a
fork; reconcile.py is the only script that acts on the result.

    ./plan.py                       # human readable, with diffs
    ./plan.py --json                # the plan reconcile.py consumes
    ./plan.py --manifest other.yaml

Exit status is 1 when the manifest cannot be released, so the same
invocation doubles as the pull request check.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from common import (
    COMPONENTS_FILE,
    MANIFEST_FILE,
    Component,
    Config,
    GitHub,
    Manifest,
    ManifestEntry,
    MissingKey,
    Version,
    bump_branch,
    bump_kind_between,
    load_config,
    load_manifest,
    log,
    replace_yaml_value,
)

PR_BODY_TEMPLATE = """\
Release **{component} {version}** from `{branch}`.

{summary}

Merging this pull request pushes `{files}`, which is what
`.github/workflows/release.yaml` watches. The release itself is cut by
`git-release.yaml`; nothing else here needs a human.

Part of the {train_label} release train.
"""


@dataclass
class FileEdit:
    path: str
    before: str
    after: str
    # What each edit is for, so the pull request body explains itself.
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.before != self.after

    def diff(self) -> str:
        return "".join(
            difflib.unified_diff(
                self.before.splitlines(keepends=True),
                self.after.splitlines(keepends=True),
                fromfile=f"a/{self.path}",
                tofile=f"b/{self.path}",
                n=1,
            )
        )


@dataclass
class ComponentPlan:
    name: str
    repo: str
    tier: int
    base_branch: str
    head_branch: str
    current_version: str | None
    version: str
    bump_kind: str
    is_hotfix: bool
    depends_on: list[str]
    waits_for_images: list[dict]
    files: list[FileEdit]
    pr_title: str
    pr_body: str
    labels: list[str]


class Planner:
    def __init__(self, config: Config, github: GitHub):
        self.config = config
        self.github = github
        self.problems: list[str] = []
        self.warnings: list[str] = []
        # Components that failed validation, so their dependents are not
        # planned against versions that are never going to exist.
        self.failed: set[str] = set()
        # repo -> path -> text, so a repository is read once even when
        # several bumps land in the same file.
        self._files: dict[tuple[str, str, str], str | None] = {}

    # -- helpers ------------------------------------------------------

    def file_text(self, repo: str, path: str, ref: str) -> str | None:
        key = (repo, path, ref)
        if key not in self._files:
            self._files[key] = self.github.file_text(repo, path, ref)
        return self._files[key]

    def problem(self, message: str) -> None:
        self.problems.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    # -- validation ---------------------------------------------------

    def validate_entry(self, entry: ManifestEntry, component: Component) -> str | None:
        """Check one component. Returns the version it is moving from."""
        if entry.branch not in self.config.release_branches:
            self.problem(
                f"{entry.name}: branch {entry.branch!r} is not one of "
                + ", ".join(self.config.release_branches)
            )
            return None

        if not self.github.branch_exists(component.repo, entry.branch):
            self.problem(f"{entry.name}: {component.repo} has no {entry.branch!r} branch")
            return None

        current_text = self.file_text(component.repo, component.version_file, entry.branch)
        if current_text is None:
            self.problem(
                f"{entry.name}: {component.repo}:{entry.branch} has no "
                f"{component.version_file} file"
            )
            return None

        current = Version.parse_or_none(current_text.strip())
        if current is None:
            self.problem(
                f"{entry.name}: {component.version_file} on {entry.branch} holds "
                f"{current_text.strip()!r}, which is not a semantic version"
            )
            return None

        if not entry.version > current:
            self.problem(
                f"{entry.name}: {entry.version} does not advance on {current} "
                f"(current {component.version_file} on {entry.branch})"
            )
            return None

        # A tag that already exists means the release ran and the manifest
        # is stale; opening the bump would fail late and confusingly.
        if self.github.release_by_tag(component.repo, str(entry.version)) is not None:
            self.problem(f"{entry.name}: {entry.version} is already released on GitHub")
            return None

        kind = bump_kind_between(current, entry.version)
        if entry.branch == "release" and kind != "patch":
            self.problem(
                f"{entry.name}: a hotfix off `release` must be a patch bump, "
                f"but {current} -> {entry.version} is a {kind} bump"
            )
            return None

        latest = self.github.latest_release(component.repo)
        latest_version = Version.parse_or_none((latest or {}).get("tag_name"))
        if entry.branch == "main" and latest_version and not entry.version > latest_version:
            self.problem(
                f"{entry.name}: {entry.version} is not ahead of the latest "
                f"release {latest_version}"
            )
            return None

        return str(current)

    def validate_dependencies(self, manifest: Manifest) -> None:
        for name in manifest.entries:
            component = self.config.components.get(name)
            if component is None:
                continue
            for dependency in component.depends_on:
                if dependency in manifest.entries:
                    if self.config.components[dependency].tier >= component.tier:
                        self.problem(
                            f"{name} (tier {component.tier}) depends on {dependency} "
                            f"(tier {self.config.components[dependency].tier}); a "
                            "dependency must sit in an earlier tier"
                        )

        # The reverse direction is a warning, not an error: releasing web
        # without re-releasing the chart is legal, it just leaves the
        # chart pinned to the previous image.
        for name, component in self.config.components.items():
            if name in manifest.entries:
                continue
            stale = [dep for dep in component.depends_on if dep in manifest.entries]
            if stale:
                self.warn(
                    f"{name} is not in the train but pins "
                    + ", ".join(sorted(stale))
                    + f"; it will keep pointing at the previous {'version' if len(stale) == 1 else 'versions'}"
                )

    # -- file edits ---------------------------------------------------

    def version_for(self, source: str | None, entry: ManifestEntry, manifest: Manifest) -> str | None:
        """The version a single bump should write.

        A bump with no `source` writes the component's own version. A
        bump naming another component writes that component's version
        only when it is part of this train; otherwise the pin is left
        exactly as it is.
        """
        if source is None:
            return str(entry.version)
        other = manifest.entries.get(source)
        return str(other.version) if other else None

    def build_edits(
        self, entry: ManifestEntry, component: Component, manifest: Manifest
    ) -> list[FileEdit]:
        edits: dict[str, FileEdit] = {}

        for bump in component.bumps:
            value = self.version_for(bump.source, entry, manifest)
            if value is None:
                continue

            original = self.file_text(component.repo, bump.path, entry.branch)
            if original is None:
                self.problem(
                    f"{entry.name}: {component.repo}:{entry.branch} has no {bump.path}"
                )
                continue

            edit = edits.get(bump.path) or FileEdit(path=bump.path, before=original, after=original)
            owner = bump.source or entry.name

            if bump.kind == "plain":
                suffix = "\n" if edit.after.endswith("\n") else ""
                edit.after = f"{value}{suffix}"
                edit.notes.append(f"{bump.path} -> {value}")
            elif bump.kind == "yaml":
                try:
                    edit.after = replace_yaml_value(edit.after, bump.key, value)
                except MissingKey:
                    self.problem(
                        f"{entry.name}: {bump.path} has no key {bump.key!r}; "
                        "components.yaml is out of date with the chart"
                    )
                    continue
                edit.notes.append(f"{bump.path} `{bump.key}` -> {value} ({owner})")
            else:
                self.problem(f"{entry.name}: unknown bump kind {bump.kind!r} for {bump.path}")
                continue

            edits[bump.path] = edit

        return [edit for edit in edits.values() if edit.changed]

    # -- plan ---------------------------------------------------------

    def build(self, manifest: Manifest) -> dict:
        for name in manifest.entries:
            if name not in self.config.components:
                self.problem(
                    f"unknown component {name!r}; known: "
                    + ", ".join(sorted(self.config.components))
                )

        self.validate_dependencies(manifest)

        plans: list[ComponentPlan] = []
        # components.yaml order inside a tier, so the plan reads the same
        # way every time.
        ordered = [
            (name, self.config.components[name])
            for name in self.config.components
            if name in manifest.entries
        ]
        ordered.sort(key=lambda item: (item[1].tier, list(self.config.components).index(item[0])))

        train = manifest.train or self.infer_train(manifest)

        for name, component in ordered:
            entry = manifest.entries[name]
            log(f"planning {name} {entry.version} on {entry.branch}")

            blocked = sorted(self.failed.intersection(component.depends_on))
            if blocked:
                self.problem(f"{name}: cannot be planned, it depends on " + ", ".join(blocked))
                self.failed.add(name)
                continue

            current = self.validate_entry(entry, component)
            if current is None:
                self.failed.add(name)
                continue

            edits = self.build_edits(entry, component, manifest)
            if not edits:
                self.problem(f"{name}: nothing to change, which cannot be right")
                self.failed.add(name)
                continue

            waits = [
                {"component": dependency, "image": self.config.components[dependency].ghcr_image,
                 "tag": str(manifest.entries[dependency].version)}
                for dependency in component.depends_on
                if dependency in manifest.entries
                and self.config.components[dependency].ghcr_image
            ]

            summary = "\n".join(f"- {note}" for edit in edits for note in edit.notes)
            plans.append(
                ComponentPlan(
                    name=name,
                    repo=component.repo,
                    tier=component.tier,
                    base_branch=entry.branch,
                    head_branch=bump_branch(entry.version),
                    current_version=current,
                    version=str(entry.version),
                    bump_kind=bump_kind_between(Version.parse(current), entry.version),
                    is_hotfix=entry.branch == "release",
                    depends_on=[d for d in component.depends_on if d in manifest.entries],
                    waits_for_images=waits,
                    files=edits,
                    pr_title=f"Release {name} {entry.version}",
                    pr_body=PR_BODY_TEMPLATE.format(
                        component=name,
                        version=entry.version,
                        branch=entry.branch,
                        summary=summary,
                        files=", ".join(edit.path for edit in edits),
                        train_label=train or "current",
                    ),
                    labels=["release", "skip-release-notes"],
                )
            )

        return {
            "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "train": train,
            "valid": not self.problems,
            "problems": self.problems,
            "warnings": self.warnings,
            "tiers": sorted({plan.tier for plan in plans}),
            "components": [asdict(plan) for plan in plans],
        }

    def infer_train(self, manifest: Manifest) -> str | None:
        """release.adoc names a train after web's X.Y."""
        entry = manifest.entries.get("web")
        return f"{entry.version.major}.{entry.version.minor}" if entry else None


def render_human(plan: dict) -> str:
    lines: list[str] = []
    train = plan["train"]
    lines.append(f"Release train {train}" if train else "Release train")
    lines.append("")

    if not plan["components"]:
        lines.append("Nothing to release.")
    for tier in plan["tiers"]:
        members = [c for c in plan["components"] if c["tier"] == tier]
        lines.append(f"Tier {tier}")
        for component in members:
            waits = component["waits_for_images"]
            suffix = (
                "  (waits for " + ", ".join(f"{w['image']}:{w['tag']}" for w in waits) + ")"
                if waits
                else ""
            )
            lines.append(
                f"  {component['name']:<12} {component['current_version']} -> "
                f"{component['version']}  [{component['bump_kind']}]  "
                f"{component['repo']}:{component['base_branch']} <- "
                f"{component['head_branch']}{suffix}"
            )
            for edit in component["files"]:
                for note in edit["notes"]:
                    lines.append(f"      {note}")
        lines.append("")

    for warning in plan["warnings"]:
        lines.append(f"warning: {warning}")
    for problem in plan["problems"]:
        lines.append(f"problem: {problem}")
    if plan["problems"]:
        lines.append("")
        lines.append("The manifest cannot be released as it stands.")
    return "\n".join(lines).rstrip() + "\n"


def render_diffs(plan: dict) -> str:
    chunks = []
    for component in plan["components"]:
        for edit in component["files"]:
            diff = "".join(
                difflib.unified_diff(
                    edit["before"].splitlines(keepends=True),
                    edit["after"].splitlines(keepends=True),
                    fromfile=f"a/{component['repo']}/{edit['path']}",
                    tofile=f"b/{component['repo']}/{edit['path']}",
                    n=1,
                )
            )
            if diff:
                chunks.append(diff if diff.endswith("\n") else diff + "\n")
    return "".join(chunks)


def build_plan(
    manifest_path: Path = MANIFEST_FILE, components_path: Path = COMPONENTS_FILE
) -> dict:
    config = load_config(components_path)
    github = GitHub(config.github_api, config.github_org, dry_run=True)
    return Planner(config, github).build(load_manifest(manifest_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--manifest", type=Path, default=MANIFEST_FILE)
    parser.add_argument("--components", type=Path, default=COMPONENTS_FILE)
    parser.add_argument("--json", action="store_true", help="machine readable plan on stdout")
    parser.add_argument("-o", "--output", type=Path, help="also write the JSON plan here")
    parser.add_argument("--no-diff", action="store_true", help="summary only")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    args = parser.parse_args(argv)

    plan = build_plan(args.manifest, args.components)

    if args.output:
        args.output.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        log(f"wrote {args.output}")

    if args.json:
        json.dump(plan, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_human(plan))
        if not args.no_diff:
            diffs = render_diffs(plan)
            if diffs:
                sys.stdout.write("\n")
                sys.stdout.write(diffs)

    if plan["problems"]:
        return 1
    return 1 if args.strict and plan["warnings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
