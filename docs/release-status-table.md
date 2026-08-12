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
| `scripts/release/common.py` | the shared library the two import: configuration, a read-only GitHub client, and the OBS and SCC readers |

`components.yaml` is the single source of truth. Adding a component
means adding an entry there and nothing else.

## Running it

```bash
python3 -m venv .venv
.venv/bin/pip install -r scripts/release/requirements.txt

export TRENTO_OBS_PROJECT_STABLE=devel:sap:trento
export TRENTO_OBS_PROJECT_ROLLING=devel:sap:trento:factory

.venv/bin/python scripts/release/collect_state.py -o state.json
.venv/bin/python scripts/release/render_table.py --state state.json
```

That prints the table. Add `--inject profile/README.md` to write it in
place. `GITHUB_TOKEN` is optional and only raises the API rate limit;
the scripts fall back to the `gh` CLI's own credentials when it is set
up, and work anonymously when it is not.

The two exports are what the workflow passes from the organisation
variables. Without them the run says so and the table comes out with no
OBS columns, which is the correct answer for somewhere that publishes
to no OBS project.

A fork renders its own table. The workflow passes
`TRENTO_GITHUB_ORG: ${{ github.repository_owner }}`, so a fork's run
reports the fork's releases rather than `trento-project`'s and does not
produce a diff that looks like upstream regressed. The OBS projects
cannot follow a fork the same way, because OBS is keyed on an OBS
account and nothing connects that to a GitHub owner. A fork that
publishes to `home:<user>:trento` says so by setting its own two
variables, and one that publishes nowhere gets no OBS columns.

## Variables and secrets

Nothing new is introduced. Both of these already exist for the component
release workflows.

| | |
| --- | --- |
| `OBS_PROJECT_STABLE`, `OBS_PROJECT_ROLLING` | organisation variables. Every component's obs-sync matrix is built from them, and the table reads the same two, so it cannot name a project nothing publishes to. **The table's OBS columns depend on them**: with neither in scope there are no OBS columns |
| `TRENTOBOT_GPG_KEY` | the key every component's release commits are signed with. Used here for the same reason: a commit that lands on the organisation profile unattended should be verifiable |

Both must be visible to this repository, not only to the component
repositories. An organisation secret or variable restricted to selected
repositories will not reach `.github` unless it is on the list.

The project names are deliberately not repeated in `components.yaml`.
A second copy is a copy to fall out of step, and it is what would let a
fork with no variables of its own report this organisation's builds as
if they were the fork's.

The `obs_package` of each component restates the repository's own
`OBS_PACKAGE` variable, which is where obs-sync.yaml reads it. If the
two ever part company the table reports a package nothing publishes to
and goes on calling it "not submitted" forever, so they are worth
keeping in step.

Reading the three APIs needs no credential at all. The key is only for
signing the commit, and where it is not in scope — a fork, or a local
run — the commit is made by the Actions bot instead and the table is no
different for it.
