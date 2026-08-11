<!--
SPDX-FileCopyrightText: SUSE LLC
SPDX-License-Identifier: Apache-2.0
-->

# The release status table

`profile/README.md` carries a table of what is released where, between
markers, refreshed daily by `.github/workflows/release-table.yaml`.
Answering "which version of Trento is in 15 SP7?" should not require
opening three websites.

## Where the numbers come from

Three public APIs, none of which needs credentials:

| | |
| --- | --- |
| GitHub | the latest release per repository |
| OBS | `devel:sap:trento` and `devel:sap:trento:factory`, read straight from `/public/source` |
| SCC | which SLES for SAP product ships which version |

No version, package or service pack is written by hand anywhere. The
SLES columns are not configured either: products come from SCC and are
filtered to those actually shipping a Trento package, so a new service
pack appears in the table on its own. SCC reports a product's version
as a display string — `SLES_SAP/15.7` calls itself "15 SP7" — so the
identifier is parsed instead.

Internal (IBS) codestreams are deliberately absent. They live in the
private release repository, so this one stays publishable.

## The pieces

| | |
| --- | --- |
| `release/components.yaml` | the component registry: repository, OBS package, SCC package, and where each source lives |
| `scripts/release/collect_state.py` | queries the three APIs and writes `state.json` |
| `scripts/release/render_table.py` | turns `state.json` into markdown and injects it between the markers |
| `scripts/release/check_variables.py` | compares `obs_package` against each repository's `OBS_PACKAGE` variable |
| `scripts/release/common.py` | the shared library the above import: configuration, a read-only GitHub client, and the OBS and SCC readers |

`components.yaml` is the single source of truth. Adding a component
means adding an entry there and nothing else.

## Running it

```bash
python3 -m venv .venv
.venv/bin/pip install -r scripts/release/requirements.txt

.venv/bin/python scripts/release/collect_state.py -o state.json
.venv/bin/python scripts/release/render_table.py --state state.json
```

That prints the table. Add `--inject profile/README.md` to write it in
place. `GITHUB_TOKEN` is optional and only raises the API rate limit;
the scripts fall back to the `gh` CLI's own credentials when it is set
up, and work anonymously when it is not.

A fork renders its own table. The workflow passes
`TRENTO_GITHUB_ORG: ${{ github.repository_owner }}`, so a fork's run
reports the fork's releases rather than `trento-project`'s and does not
produce a diff that looks like upstream regressed.

## Variables

Nothing new is introduced.

| | |
| --- | --- |
| `OBS_PROJECT_STABLE`, `OBS_PROJECT_ROLLING` | organisation variables. Every component's obs-sync matrix is built from them, and the table reads them, so the table cannot name a project nothing publishes to |
| `OBS_PACKAGE` | a repository variable, one per component. Restated in `components.yaml` as `obs_package`, which is what `check_variables.py` compares |
| `TRENTOBOT_GH_PAT` | the organisation bot's token, already used by `git-release.yaml`, `publish-containers.yaml` and `obs-sync.yaml` |

The two OBS project names are also in `components.yaml`. That copy is
the fallback for a fork or a local run, where no organisation variable
is in scope; where one is, it wins.

`check_variables.py` is what keeps `obs_package` and `OBS_PACKAGE` from
parting company. A disagreement is otherwise silent: the table would
report an OBS package nothing publishes to, and go on calling it "not
submitted" forever. It runs beside the table and needs a token that can
read repository variables; without one it says so and passes, which is
what happens on a fork.

The table itself needs no token and no secret.
