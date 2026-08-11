#!/usr/bin/env python3
# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

"""Drive the release cascade one step at a time.

Stateless by design: every run derives the whole state of the train from
GitHub and GHCR, decides the single next thing to do per component, and
does it. Nothing is remembered between runs, so a lost run, a rerun or a
concurrent run all converge to the same place. That is also why the
component repositories need no changes: they are never notified, they
are only observed.

Per component the state is one of

  todo        no bump pull request yet
  blocked     an upstream component has not published its image
  open        bump pull request is open, waiting for a human to merge
  merged      merged; waiting for git-release.yaml to publish the tag
  released    the tag exists, and the container image too where there is one
  abandoned   the bump pull request was closed unmerged; needs a human

The manifest pull request is merged only once every component reads
`released`.

    ./reconcile.py                      # dry run against the local manifest
    ./reconcile.py --pr 42              # dry run, reporting on that pull request
    ./reconcile.py --pr 42 --execute    # the only invocation that writes

`--execute` still refuses to act unless the manifest pull request is
approved and carries the `release/go` label.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from common import (
    COMPONENTS_FILE,
    MANIFEST_FILE,
    SELF_REPO,
    Config,
    GitHub,
    ghcr_tag_exists,
    load_config,
    log,
)
from plan import build_plan

COMMENT_MARKER = "<!-- trento-release-cascade -->"
GO_LABEL = "release/go"
DONE_LABEL = "release/done"
BLOCKED_LABEL = "release/blocked"
MANIFEST_PATH = "release/manifest.yaml"

STATE_ICON = {
    "released": "✅",
    "merged": "🟦",
    "open": "🟨",
    "blocked": "⏸️",
    "todo": "⬜",
    "abandoned": "❌",
}


def find_manifest_pull(github: GitHub) -> dict | None:
    """The open pull request that edits the manifest, if there is one."""
    for pull in github.open_pulls(SELF_REPO):
        if MANIFEST_PATH in github.pull_files(SELF_REPO, pull["number"]):
            return pull
    return None


class Cascade:
    def __init__(self, config: Config, github: GitHub, plan: dict):
        self.config = config
        self.github = github
        self.plan = plan
        self.states: dict[str, dict] = {}

    # -- observation --------------------------------------------------

    def image_ready(self, component: dict) -> bool | None:
        """None when the component publishes no image."""
        image = self.config.components[component["name"]].ghcr_image
        if not image:
            return None
        return ghcr_tag_exists(self.config, image, component["version"])

    def observe(self, component: dict) -> dict:
        name = component["name"]
        repo = component["repo"]
        version = component["version"]

        released = self.github.release_by_tag(repo, version) is not None
        image = self.image_ready(component) if released else None

        pulls = self.github.pulls_for_head(repo, component["head_branch"])
        pull = pulls[0] if pulls else None

        if released and image is not False:
            state = "released"
        elif released:
            state = "merged"  # tag is out, image still building
        elif pull and pull.get("merged_at"):
            state = "merged"
        elif pull and pull.get("state") == "open":
            state = "open"
        elif pull:
            state = "abandoned"
        else:
            waiting = [
                dependency
                for dependency in component["depends_on"]
                if self.states.get(dependency, {}).get("state") != "released"
            ]
            state = "blocked" if waiting else "todo"

        return {
            "name": name,
            "state": state,
            "version": version,
            "pull": pull["number"] if pull else None,
            "pull_url": pull["html_url"] if pull else None,
            "image_ready": image,
            "waiting_on": [
                dependency
                for dependency in component["depends_on"]
                if self.states.get(dependency, {}).get("state") != "released"
            ],
        }

    # -- action -------------------------------------------------------

    def open_bump(self, component: dict, trigger: str) -> None:
        files = {edit["path"]: edit["after"] for edit in component["files"]}
        message = f"Release {component['name']} {component['version']}"
        self.github.commit_files(
            component["repo"],
            component["head_branch"],
            component["base_branch"],
            files,
            message,
        )
        body = component["pr_body"] + (f"\nRequested by {trigger}.\n" if trigger else "")
        pull = self.github.create_pull(
            component["repo"],
            component["head_branch"],
            component["base_branch"],
            component["pr_title"],
            body,
        )
        if pull:
            self.github.add_labels(component["repo"], pull["number"], component["labels"])

    def step(self, trigger: str, execute: bool) -> list[dict]:
        for component in self.plan["components"]:
            observed = self.observe(component)
            self.states[component["name"]] = observed
            log(f"  {component['name']}: {observed['state']}")

            if observed["state"] == "todo":
                if execute:
                    self.open_bump(component, trigger)
                    # Re-observing would cost another round trip for no
                    # gain: the next run picks it up as `open`.
                    observed["state"] = "open"
                else:
                    self.github.performed.append(
                        f"open bump pull request {component['repo']} "
                        f"{component['head_branch']} -> {component['base_branch']}"
                    )
                    log(f"  [dry-run] would open bump pull request for {component['name']}")

        return [self.states[c["name"]] for c in self.plan["components"]]

    # -- reporting ----------------------------------------------------

    def comment(self, observations: list[dict], gate: list[str]) -> str:
        lines = [
            COMMENT_MARKER,
            f"### Release train {self.plan['train'] or ''}".rstrip(),
            "",
            "| | Component | Version | Bump pull request | State |",
            "| --- | --- | --- | --- | --- |",
        ]
        by_name = {c["name"]: c for c in self.plan["components"]}
        for observed in observations:
            component = by_name[observed["name"]]
            pull = (
                f"[{component['repo']}#{observed['pull']}]({observed['pull_url']})"
                if observed["pull"]
                else "—"
            )
            detail = observed["state"]
            if observed["state"] == "blocked":
                detail += " on " + ", ".join(observed["waiting_on"])
            if observed["state"] == "merged" and observed["image_ready"] is False:
                detail += ", image not published yet"
            lines.append(
                f"| {STATE_ICON.get(observed['state'], '')} | `{observed['name']}` "
                f"| `{observed['version']}` | {pull} | {detail} |"
            )

        lines.append("")
        for problem in self.plan["problems"]:
            lines.append(f"- 🛑 {problem}")
        for warning in self.plan["warnings"]:
            lines.append(f"- ⚠️ {warning}")
        for reason in gate:
            lines.append(f"- ⏳ {reason}")
        if not gate and not self.plan["problems"]:
            remaining = [o for o in observations if o["state"] != "released"]
            lines.append(
                "All components released; this pull request can be merged."
                if not remaining
                else f"Waiting on {len(remaining)} component(s)."
            )
        lines.append("")
        lines.append(
            f"<sub>Reconciled at {self.plan['generated_at']}. This comment is rewritten in place.</sub>"
        )
        return "\n".join(lines)


def gate_reasons(github: GitHub, pull: dict | None, plan: dict, execute: bool) -> list[str]:
    """Everything standing between the plan and the orchestrator acting."""
    reasons: list[str] = []
    if plan["problems"]:
        reasons.append("the manifest does not validate")
    if not plan["components"]:
        reasons.append("the manifest lists no components")
    if pull is None:
        reasons.append("no open manifest pull request was found")
        return reasons
    if GO_LABEL not in github.labels_on(SELF_REPO, pull["number"]):
        reasons.append(f"the `{GO_LABEL}` label is not set")
    if not github.is_approved(SELF_REPO, pull["number"]):
        reasons.append("the pull request is not approved")
    if not execute:
        reasons.append("running in dry-run mode")
    return reasons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--manifest", type=Path, default=MANIFEST_FILE)
    parser.add_argument("--components", type=Path, default=COMPONENTS_FILE)
    parser.add_argument("--pr", type=int, help="manifest pull request number; discovered if omitted")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually open pull requests and merge; without it nothing is written",
    )
    parser.add_argument("--no-comment", action="store_true", help="do not touch the pull request")
    parser.add_argument("--json", action="store_true", help="machine readable result on stdout")
    args = parser.parse_args(argv)

    config = load_config(args.components)
    github = GitHub(config.github_api, config.github_org, dry_run=not args.execute)

    log("building the plan")
    plan = build_plan(args.manifest, args.components)

    pull = (
        github.pull(SELF_REPO, args.pr)
        if args.pr
        else find_manifest_pull(github)
    )
    if pull:
        log(f"manifest pull request: {SELF_REPO}#{pull['number']} {pull['title']}")

    gate = gate_reasons(github, pull, plan, args.execute)
    act = not gate
    if gate:
        log("not acting: " + "; ".join(gate))

    log("reconciling")
    cascade = Cascade(config, github, plan)
    observations = cascade.step(
        trigger=f"{config.github_org}/{SELF_REPO}#{pull['number']}" if pull else "",
        execute=act,
    )

    finished = bool(observations) and all(o["state"] == "released" for o in observations)
    blocked = any(o["state"] == "abandoned" for o in observations)

    if pull and not args.no_comment:
        github.upsert_comment(SELF_REPO, pull["number"], COMMENT_MARKER, cascade.comment(observations, gate))
        labels = github.labels_on(SELF_REPO, pull["number"])
        if blocked and BLOCKED_LABEL not in labels:
            github.add_labels(SELF_REPO, pull["number"], [BLOCKED_LABEL])
        if not blocked and BLOCKED_LABEL in labels:
            github.remove_label(SELF_REPO, pull["number"], BLOCKED_LABEL)
        if finished and act:
            github.add_labels(SELF_REPO, pull["number"], [DONE_LABEL])
            github.merge_pull(
                SELF_REPO,
                pull["number"],
                title=f"Release train {plan['train']}" if plan["train"] else "",
            )

    result = {
        "train": plan["train"],
        "acting": act,
        "gate": gate,
        "finished": finished,
        "components": observations,
        "actions": github.performed,
    }
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(cascade.comment(observations, gate).replace(COMMENT_MARKER + "\n", ""))
        sys.stdout.write("\n")

    return 1 if plan["problems"] or blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
