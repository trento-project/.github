#!/usr/bin/env python3
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

"""Propose the next release train from what has been merged.

For every component this looks at the pull requests merged since its
last release and resolves a bump type from their labels, using the very
same `version-resolver` mapping release-drafter uses to write the
changelog. Reading that file at runtime rather than restating it here
means the proposed version and the generated release notes can never
disagree.

The result is a manifest ready to be edited and opened as a pull
request. Nothing is written to GitHub.

    ./propose.py                        # manifest on stdout
    ./propose.py -o release/manifest.yaml
    ./propose.py --branch release       # propose hotfixes instead
    ./propose.py --only web --only wanda
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from common import (
    COMPONENTS_FILE,
    MANIFEST_FILE,
    REPO_ROOT,
    Component,
    Config,
    GitHub,
    Version,
    load_config,
    load_yaml,
    log,
)

# Where release-drafter keeps the mapping. Same file the release
# workflow passes as `config-name`.
RESOLVER_FILE = {
    "main": REPO_ROOT / ".github" / "release_drafter_main.yaml",
    "release": REPO_ROOT / ".github" / "release_drafter_release.yaml",
}

RANK = {"patch": 0, "minor": 1, "major": 2}


@dataclass
class Resolver:
    """The label -> bump mapping, read out of release-drafter's config."""

    by_label: dict[str, str]
    default: str
    excluded: set[str]

    @classmethod
    def load(cls, path: Path) -> "Resolver":
        raw = load_yaml(path) or {}
        resolver = raw.get("version-resolver") or {}
        by_label: dict[str, str] = {}
        for kind in ("major", "minor", "patch"):
            for label in (resolver.get(kind) or {}).get("labels") or []:
                by_label[str(label)] = kind
        return cls(
            by_label=by_label,
            default=str(resolver.get("default", "patch")),
            excluded={str(label) for label in raw.get("exclude-labels") or []},
        )

    def kind_for(self, labels: set[str]) -> str | None:
        """None when the pull request is excluded from the release notes."""
        if labels & self.excluded:
            return None
        kinds = [self.by_label[label] for label in labels if label in self.by_label]
        return max(kinds, key=lambda kind: RANK[kind]) if kinds else self.default


class Versions:
    """The current version of each component, read once.

    Both the direct proposals and the dependents dragged in afterwards
    need it, and a component with no parseable version file should say
    so once rather than once per caller.
    """

    def __init__(self, github: GitHub, branch: str):
        self.github = github
        self.branch = branch
        self._cache: dict[str, Version | None] = {}

    def of(self, component: Component) -> Version | None:
        if component.name not in self._cache:
            text = self.github.file_text(component.repo, component.version_file, self.branch)
            version = Version.parse_or_none((text or "").strip())
            if version is None:
                log(f"  ! {component.name}: no usable {component.version_file} on {self.branch}")
            self._cache[component.name] = version
        return self._cache[component.name]


@dataclass
class Proposal:
    name: str
    branch: str
    current: Version
    version: Version
    kind: str
    pull_count: int
    reasons: list[str] = field(default_factory=list)
    label_counts: dict[str, int] = field(default_factory=dict)
    # Set when the component is dragged in by a dependency rather than
    # by its own merged pull requests.
    pulled_in_by: list[str] = field(default_factory=list)


def merged_pulls(github: GitHub, component: Component, branch: str, since: str | None) -> list[dict]:
    query = f"repo:{github.org}/{component.repo} is:pr is:merged base:{branch}"
    if since:
        query += f" merged:>{since}"
    return github.search_pulls(query)


def propose_component(
    github: GitHub, versions: Versions, component: Component, branch: str, resolver: Resolver
) -> Proposal | None:
    current = versions.of(component)
    if current is None:
        return None

    since = github.commit_date(component.repo, str(current))
    pulls = merged_pulls(github, component, branch, since)

    kinds: list[str] = []
    reasons: list[str] = []
    label_counts: dict[str, int] = {}
    for pull in pulls:
        labels = {label["name"] for label in pull.get("labels", [])}
        for label in labels:
            label_counts[label] = label_counts.get(label, 0) + 1
        kind = resolver.kind_for(labels)
        if kind is None:
            continue
        kinds.append(kind)
        if RANK[kind] > 0:
            reasons.append(f"#{pull['number']} {kind} ({', '.join(sorted(labels)) or 'no labels'})")

    if not kinds:
        return None

    kind = max(kinds, key=lambda item: RANK[item])
    if branch == "release":
        # git-release.yaml's hotfix path only ever cuts a patch.
        kind = "patch"

    return Proposal(
        name=component.name,
        branch=branch,
        current=current,
        version=current.bump(kind),
        kind=kind,
        pull_count=len(kinds),
        reasons=reasons,
        label_counts=label_counts,
    )


def pull_in_dependents(
    versions: Versions, config: Config, proposals: dict[str, Proposal], branch: str
) -> None:
    """Re-cut anything that pins a component being released.

    The chart carries image tags, so leaving it out of the train would
    publish a release whose chart still points at the previous images.
    A component already in the train is not left alone either: its bump
    is raised to the largest bump of what it pins, so a minor web gives
    a minor chart rather than the patch its own one chore pull request
    would have earned.
    """
    for name, component in config.components.items():
        triggers = sorted(set(component.depends_on) & set(proposals))
        if not triggers:
            continue

        existing = proposals.get(name)
        inherited = max((proposals[t].kind for t in triggers), key=lambda item: RANK[item])
        kind = "patch" if branch == "release" else inherited
        if existing and RANK[existing.kind] >= RANK[kind]:
            continue

        if existing:
            current = existing.current
            pull_count = existing.pull_count
            label_counts = existing.label_counts
        else:
            parsed = versions.of(component)
            if parsed is None:
                continue
            current, pull_count, label_counts = parsed, 0, {}

        proposals[name] = Proposal(
            name=name,
            branch=branch,
            current=current,
            version=current.bump(kind),
            kind=kind,
            pull_count=pull_count,
            label_counts=label_counts,
            pulled_in_by=triggers,
        )


def manifest_header(path: Path) -> str:
    """Everything above `train:`, so the manifest keeps its instructions."""
    if not path.exists():
        return ""
    kept: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        if line.startswith("train:") or line.startswith("components:"):
            break
        kept.append(line)
    return "".join(kept)


def render_manifest(header: str, proposals: dict[str, Proposal], config: Config) -> str:
    web = proposals.get("web")
    train = f"{web.version.major}.{web.version.minor}" if web else None

    lines = [header.rstrip("\n"), "", f"train: {train or 'null'}", "", "components:"]
    if not proposals:
        lines[-1] = "components: {}"
        return "\n".join(lines) + "\n"

    for name in config.components:
        proposal = proposals.get(name)
        if proposal is None:
            continue
        top = sorted(proposal.label_counts.items(), key=lambda item: (-item[1], item[0]))[:4]
        parts = []
        if proposal.pull_count:
            parts.append(
                f"{proposal.pull_count} pull request"
                f"{'' if proposal.pull_count == 1 else 's'} since {proposal.current}"
                + (" (" + ", ".join(f"{count} {label}" for label, count in top) + ")" if top else "")
            )
        if proposal.pulled_in_by:
            parts.append("pins " + ", ".join(proposal.pulled_in_by))
        note = "; ".join(parts)
        lines.append(f"  {name}:")
        lines.append(f"    # {proposal.kind}; {note}")
        lines.append(f"    version: {proposal.version}")
        if proposal.branch != "main":
            lines.append(f"    branch: {proposal.branch}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--components", type=Path, default=COMPONENTS_FILE)
    parser.add_argument("--branch", default="main", help="main for a release, release for a hotfix")
    parser.add_argument("--only", action="append", default=[], help="restrict to these components")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help=f"write the manifest here (default: stdout); use {MANIFEST_FILE.name} to stage it",
    )
    parser.add_argument("--resolver", type=Path, help="override the release-drafter config to read")
    args = parser.parse_args(argv)

    config = load_config(args.components)
    if args.branch not in config.release_branches:
        parser.error(f"--branch must be one of {', '.join(config.release_branches)}")

    resolver_path = args.resolver or RESOLVER_FILE[args.branch]
    resolver = Resolver.load(resolver_path)
    log(
        f"resolver {resolver_path.name}: "
        + ", ".join(f"{label}={kind}" for label, kind in sorted(resolver.by_label.items()))
        + f"; default {resolver.default}; excluding {', '.join(sorted(resolver.excluded)) or 'nothing'}"
    )

    github = GitHub(config.github_api, config.github_org, dry_run=True)
    versions = Versions(github, args.branch)
    selected = args.only or list(config.components)

    proposals: dict[str, Proposal] = {}
    for name in config.components:
        if name not in selected:
            continue
        component = config.components[name]
        if not github.branch_exists(component.repo, args.branch):
            log(f"  {name}: no {args.branch} branch, skipped")
            continue
        if versions.of(component) is None:
            continue
        proposal = propose_component(github, versions, component, args.branch, resolver)
        if proposal is None:
            log(f"  {name}: nothing merged since its last release")
            continue
        proposals[name] = proposal
        log(
            f"  {name}: {proposal.current} -> {proposal.version} ({proposal.kind}, "
            f"{proposal.pull_count} pull request{'' if proposal.pull_count == 1 else 's'})"
        )
        for reason in proposal.reasons[:5]:
            log(f"      {reason}")

    pull_in_dependents(versions, config, proposals, args.branch)
    for name, proposal in proposals.items():
        if proposal.pulled_in_by:
            log(f"  {name}: {proposal.current} -> {proposal.version} (pins {', '.join(proposal.pulled_in_by)})")

    document = render_manifest(manifest_header(MANIFEST_FILE), proposals, config)
    if args.output:
        args.output.write_text(document, encoding="utf-8")
        log(f"wrote {args.output}")
    else:
        sys.stdout.write(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
