<!--
SPDX-FileCopyrightText: SUSE LLC
SPDX-License-Identifier: Apache-2.0
-->

# Release automation

How a Trento release is requested, cascaded and reported.

## The problem

A release means opening a version bump pull request in each of eight
repositories, in the right order, and remembering that the helm chart
pins container image tags that must exist first. `publish-oci.yaml`
fails the chart release outright when `VERSION` and the chart version
disagree, so the chart is really a five file change that happens to
start with `VERSION`.

Separately, nobody could answer "which Trento is in 15.7" without
asking, because the answer was spread across eight GitHub release pages,
two OBS projects and SCC.

## The shape

Two files hold everything, and one of them is empty most of the time.

| | |
| --- | --- |
| `release/components.yaml` | what a component is: repository, tier, OBS package, which files carry a version |
| `release/manifest.yaml` | what to release: a version per component. Empty between releases |
| `release/labels.yaml` | the canonical label set |

No version, package name or SLES service pack is written by hand
anywhere else. Adding a component is one entry in `components.yaml`.

## Requesting a release

1. Run **Propose release**. `propose.py` looks at every component, reads
   the labels on the pull requests merged since its last release, and
   resolves a bump from them. It opens a pull request filling in
   `manifest.yaml`.
2. Edit the proposal. Change versions, delete components, set
   `branch: release` for a hotfix.
3. Get it approved and add the `release/go` label.
4. **Orchestrate release** opens the bump pull requests, tier by tier,
   and merges the manifest pull request once every component is
   released.

The manifest pull request is the release: its diff is the scope, its
approval is the authorisation, and its merge is the record.

### Why the bump type is not decided here

`propose.py` reads the `version-resolver` mapping out of
`.github/release_drafter_main.yaml` at run time rather than restating
it. The version proposed and the release notes generated therefore
cannot disagree; if they ever do, one file is wrong rather than two.

This is also how two long-standing bugs surfaced. `feature` was in the
resolver's `minor` list and had never been used by a single pull
request, while `enhancement` — used 1433 times — was not, so every
release resolved as a patch. `released-as-hotfix` was in the exclusion
list and `backport-as-hotfix`, used 44 times, was not, so hotfixes were
listed twice. Both are fixed.

## Tiers

Tier 1 is everything that releases independently. Tier 2 is currently
just the helm chart.

The chart waits for the container images of what it pins to appear on
GHCR, not merely for their GitHub releases. A chart that references an
unpublished tag is broken in a way that only shows up at install time.
Its bump also carries all nine files in one commit, because a
half-applied chart fails `publish-oci.yaml`'s version equality check.

A component that pins another is dragged into the train even when it has
no changes of its own, and its bump is raised to match: a minor web
gives a minor chart, not the patch its own single chore pull request
would have earned.

## The reconciler

`reconcile.py` is stateless. Every run derives the whole state of the
train from GitHub and GHCR and does the next thing:

```
todo -> open -> merged -> released
         \
          blocked (an upstream image is not published)
```

Nothing is remembered between runs, so a missed run, a rerun and two
concurrent runs all converge. That is why the component repositories are
never notified and need no changes at all — no `repository_dispatch`, no
new workflow in eight repositories, nothing to keep in step.

It refuses to act until the manifest pull request is both approved and
labelled `release/go`. Until then it only posts the plan as a comment,
rewritten in place on every run.

## Dry runs

Every script reports by default and writes only when asked.

| | |
| --- | --- |
| `plan.py` | never writes. Reads GitHub, prints the plan and the exact diffs |
| `reconcile.py` | writes only with `--execute`, and only past the approval gate |
| `sync_labels.py` | writes only with `--execute` |
| `propose.py` | writes only the manifest file it is pointed at |

Writes funnel through one client method that no-ops and logs while
`dry_run` is set, so a dry run is a property of the client rather than
something each caller has to remember. Every `workflow_dispatch`
defaults `dry_run` to true.

```bash
python3 -m venv .venv && .venv/bin/pip install -r scripts/release/requirements.txt

.venv/bin/python scripts/release/propose.py -o /tmp/manifest.yaml
.venv/bin/python scripts/release/plan.py --manifest /tmp/manifest.yaml
.venv/bin/python scripts/release/reconcile.py --manifest /tmp/manifest.yaml
.venv/bin/python scripts/release/sync_labels.py
```

### Rehearsing on forks

Three environment variables point the whole toolchain somewhere else, so
the cascade can be run for real against a set of forks before it is ever
run against `trento-project`:

| | |
| --- | --- |
| `TRENTO_GITHUB_ORG` | the organisation or user holding the component repositories |
| `TRENTO_SELF_REPO` | this repository, which cannot be called `.github` in a fork |
| `TRENTO_GHCR_NAMESPACE` | where tier 2 looks for the images it pins |

Nothing else changes: every repository, package and image name is
derived from `components.yaml`.

```bash
env TRENTO_GITHUB_ORG=someone TRENTO_SELF_REPO=their-dot-github \
    .venv/bin/python scripts/release/plan.py --manifest /tmp/manifest.yaml
```

A fork inherits tags but not releases, so `plan.py` skips its "ahead of
the latest release" check there and the reconciler reads every component
as unreleased until its bump pull request lands. That is the intended
reading: on a fork nothing has been released.

## The status table

`profile/README.md` carries a table of what is released where, between
markers, refreshed daily by `release-table.yaml`.

Three public APIs, none of which needs credentials:

| | |
| --- | --- |
| GitHub | the latest release per repository |
| OBS | `devel:sap:trento` and `devel:sap:trento:factory`, read straight from `/public/source` |
| SCC | which SLES for SAP product ships which version |

The SLES columns are not configured. Products come from SCC and are
filtered to those actually shipping a Trento package, so a new service
pack appears on its own. SCC reports a product's version as a display
string — `SLES_SAP/15.7` calls itself "15 SP7" — so the identifier is
parsed instead.

## Labels

`sync_labels.py` gives every repository the same set. It is additive:
labels it does not know about are never touched, so repositories keep
their own.

Renames are applied first and matter. Renaming a label on GitHub keeps
every existing assignment, so `tech debt` becoming `tech-debt` carries
its 76 pull requests with it. Creating `tech-debt` alongside would leave
them uncategorised in the release notes forever.

## The internal release

Everything above stops at the public release. Getting a released version
into SLES happens in IBS and srcIBS, neither of which is reachable from
a GitHub runner and neither of which can be described in a public
repository.

That half lives in the private `trento-release` repository. The two are
joined by the package name alone: `obs_package` here, the key of
`ibs-targets.yaml` there. Neither restates the other, and the internal
checklist reads this repository's `components.yaml` over HTTP at run
time rather than keeping a copy.

## What still needs a human

By design:

- deciding the versions, by editing the proposal
- approving the manifest pull request
- merging each bump pull request, so component CI is a real gate
- the internal submissions, which need the VPN and credentials

## Tokens

`RELEASE_ORCHESTRATOR_TOKEN` is the only secret, needed by
`release-orchestrate.yaml` and `labels-sync.yaml` because the built-in
token cannot reach other repositories. It needs `contents: write` and
`pull-requests: write` on the component repositories. Everything else
uses the built-in token, and the status table needs no token at all.
