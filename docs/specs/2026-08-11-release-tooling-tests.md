<!--
SPDX-FileCopyrightText: SUSE LLC
SPDX-License-Identifier: Apache-2.0
-->

# Tests for the release tooling

Design, 2026-08-11.

## The problem

`scripts/release/` is 3775 lines of Python with no tests. It opens pull
requests across eight repositories, merges them, and rewrites version
strings in nine files of the helm chart. The failure that costs
something is not a crash — it is the cascade doing the wrong thing
quietly: a chart pinned to an image that was never published, a
`released-as-hotfix` pull request counted twice, an `alerting: null` key
rewritten to empty while bumping an unrelated image tag.

None of that is currently detectable except by running a release.

Two smaller facts set the context. Trento has no Python test precedent
at all — the only other `.py` files are a copy of
`hack/gh_release_to_obs_changeset.py` in three repositories, untested —
so this establishes the convention. And the repository has no
`pyproject.toml`, no linter config and no test workflow.

## What the code already gives us

Three properties, discovered rather than designed, that decide the
shape of everything below.

**One network seam.** All 3775 lines reach the network through four
`urllib.request.urlopen` calls: `common.py:256` inside `http_get`,
`common.py:515` inside `paginate`, `common.py:637` inside `_request`
which carries every write, and `fork_obs.py:96`.

**One write seam.** `GitHub.dry_run` gates every mutation through
`_write()`, which appends a description to `GitHub.performed`.
`reconcile.py:314` already publishes that list as output. Tests assert
on it directly.

**Addressable endpoints.** `github_api`, `obs_api` and `scc_api` all
come from `components.yaml`, and seven of the eight scripts take
`--components`. A test configuration can therefore point every API at a
local server without the code knowing it is under test. GHCR and
`render_table.py` are the two exceptions, both addressed under
*Production changes*.

## Approach

Two layers, because the two things worth proving want different
mechanisms.

| layer | harness | fixtures | asserts |
| --- | --- | --- | --- |
| rules | pytest, in-process | synthetic | return values, raised exceptions |
| write safety | pytest, in-process | synthetic | `github.performed` |
| cascade | bats, subprocess against a stub server | recorded | exit code, stdout against a golden file |

The rule layer is about logic — semver ordering, the YAML line surgery,
label resolution — and wants to be fast, small and readable in a diff.
The cascade layer is about whether the command CI runs does the right
thing, so it must be black box: no patching, no mocking, the script
invoked exactly as the workflow invokes it.

That last property is also the hedge. If the scripts are ever ported to
shell, the bats layer survives unchanged.

Two write-safety cases stay in pytest despite being about behaviour
rather than logic: proving the reconciler wrote *nothing* needs an
assertion that `performed` is an empty list. Absence of a line in
stdout is a much weaker claim.

### Rejected

**Everything in pytest with `urlopen` monkeypatched.** Fastest, and
needs no production change. Rejected because it is white box — the
tests would encode that the code uses `urllib`, and would never
exercise argument parsing or exit codes. `plan.py`'s exit code is what
CI gates on.

**Everything in bats.** The rule layer would become subprocess calls
against a CLI that does not exist, or new CLIs invented to expose
internals for testing.

**Rewriting the scripts in shell first.** Considered and dropped
separately: `replace_yaml_value` depends on ruamel reporting the line
and column of a dotted key, which no shell YAML tool offers, and `sort
-V` orders `1.0.0-rc1` above `1.0.0` where semver puts it below.

## Layout

```
scripts/release/
  tests/
    conftest.py                  pytest fixtures: temp config, fake GitHub
    test_version.py              semver algebra
    test_yaml_edit.py            replace_yaml_value, get_dotted
    test_config.py               load_config, load_manifest, env overrides
    test_resolver.py             label to bump
    test_scc.py                  product_release, product_label, select_products
    test_render.py               tables, marker injection, manifest text
    test_variables.py            problems_for
    test_reconcile.py            state machine and write safety
    e2e/
      server.py                  stub HTTP server
      components.yaml            every API pointed at 127.0.0.1
      recorded/                  captured responses, keyed by host and path
      scenarios/                 per-scenario overlays shadowing recorded/
      golden/                    expected stdout per scenario
      record.sh                  re-capture recorded/ from the live APIs
      helpers.bash               server lifecycle, golden comparison
      *.bats
  requirements-dev.txt
```

## The stub server

One file, stdlib `http.server`, roughly sixty lines. It maps a request
path to a file under `recorded/<host>/<path>.json` and replays it with
the recorded status code. `Link` headers come from a sidecar file so
`paginate`'s pagination walk is exercised for real rather than asserted
against `urllib` internals.

Started once per bats file in `setup_file`, killed in `teardown_file`.
The port is written to a temporary file and `e2e/components.yaml` is
templated with it at setup.

An unknown path returns 404 **and** writes a line to stderr. A bats
helper fails the test when a miss appears, because a silent 404 is
indistinguishable from "no release yet" and would turn a broken test
green.

### Overlays

Recorded fixtures give exactly one scenario: trento-project as it
actually is. The cascade's interesting states — a blocked chart, a
hotfix, an image that never publishes — are not in that snapshot and
cannot be conjured on demand.

Each scenario therefore adds a directory shadowing only the paths it
needs to differ, checked before the base:

```
scenarios/blocked-chart/ghcr.io/v2/trento-project/trento-web/manifests/3.2.0.404
scenarios/hotfix/api.github.com/repos/trento-project/web/contents/VERSION.json
```

A scenario is a few lines of JSON rather than a second recording, so
refreshing the base with `record.sh` leaves every scenario valid.

### Recording

`record.sh` walks the same URLs the scripts request, against the real
`trento-project`, and writes them under `recorded/`. No credential is
captured, because all four APIs answer these paths anonymously — which
is already relied on today, since the status table workflow runs
tokenless.

It is not run in CI. Reaching live APIs on a schedule would turn an
upstream change into a red build on an unrelated pull request. Fixtures
going stale shows up as a diff when someone runs it.

## Unit layer

Around 60 to 70 cases, roughly 900 lines. No production code changes.

**Version algebra** (`common.py:155-231`)

- `parse` on `1.2.3`, `v1.2.3`, `1.2.3-rc1`, `1.2.3+build`, and junk
- ordering: `1.2.3-rc1 < 1.2.3`, build metadata ignored, `1.10.0 > 1.9.0`
- `bump` on each of major, minor, patch; unknown kind raises

**Label resolution** (`propose.py:54-81`)

- highest rank wins when a pull request carries both `enhancement` and `bug`
- an excluded label beats every other label and returns `None`
- no known label falls back to `default`
- regression guard: `enhancement` resolves `minor`, and
  `backport-as-hotfix` is excluded. These two assertions are the bugs
  fixed when this tooling was written; without them the next edit to
  `.github/release_drafter_main.yaml` reintroduces them silently.

**YAML line surgery** (`common.py:104-140`)

- rewrites the target line only, every other byte identical
- a document containing an explicit `null` survives untouched. This is
  the reason the function exists instead of a ruamel round-trip, and
  nothing currently proves it.
- quoted, single-quoted and bare scalars keep their quoting
- a missing key, and a key whose value is a list or block scalar, raise
  `MissingKey`

**SCC parsing** (`common.py:809-826`, `collect_state.py:79`)

- `SLES_SAP/15.7` gives `(15, 7)`, taken from the identifier rather
  than the `15 SP7` display string
- `select_products` honours `max_products_per_major` and drops products
  shipping no Trento package

**Config loading** (`common.py:341-425`)

- `TRENTO_GITHUB_ORG`, `TRENTO_SELF_REPO`, `TRENTO_GHCR_NAMESPACE`,
  `TRENTO_OBS_PROJECT_STABLE` and `TRENTO_OBS_PROJECT_ROLLING` each
  override, and absence falls back to the file
- `obs_version_file: '{obs_package}.spec'` interpolates per component
- an unknown component name raises with the known list in the message

**Reconciler** (`reconcile.py:105-120`), synthetic and in-process

- all six states: `todo`, `open`, `merged`, `released`, `blocked` when a
  dependency is not yet released, and `abandoned` when a pull request
  exists but is closed unmerged
- a published tag with a missing image stays `merged`, not `released`
- write safety: with approval absent, and separately with `release/go`
  absent, `github.performed` is an empty list

`abandoned` has no entry in `STATE_ICON` (`reconcile.py:59`), so
`STATE_ICON.get(state, '')` renders it as a blank cell in the progress
comment — a component whose bump was closed unmerged looks like a
formatting glitch rather than a stalled release. The test pins the
current behaviour and records the gap; giving it an icon is a
one-line follow-up, deliberately not bundled into a testing change.

**Remaining pure functions** — `problems_for`, `em_dash_if_empty`,
`without_timestamp`, `inject`, `render_manifest`, `render_human`,
`render_diffs`. Dict in, string out. `inject` matters beyond its size:
mismatched markers corrupt `profile/README.md`.

### Decisions this layer pins

`Version("1.2.3-rc1").bump("patch")` returns `1.2.4`, skipping `1.2.3`
— which semver ranks immediately after `1.2.3-rc1`. The test pins the
current behaviour rather than changing it: no Trento `VERSION` file
holds a prerelease today, so altering untested code here would be
speculative. The test exists so that a future decision to promote
release candidates is a deliberate edit with a visible failure, not a
silent change.

`bump_kind_between(v, v)` returning `"patch"` for two equal versions is
unreachable from the only caller: `plan.py:162` rejects a manifest
entry that does not advance on the current version, before the kind is
computed. The test therefore goes on the guard, not on the classifier.

## End-to-end layer

Around 25 scenarios, roughly 700 lines of bats plus the recorded JSON.

| file | scenario | asserts |
| --- | --- | --- |
| `plan.bats` | base plus a manifest | `--json` against `golden/plan.json`, exit 0 |
| | unknown component | exit 1, known-components list on stderr |
| | a version that does not advance | exit 1, the `plan.py:162` guard |
| | a tag already released on GitHub | exit 1, stale-manifest message |
| | a non-patch bump on `branch: release` | exit 1, the hotfix guard |
| | web in the train, chart left out | warning, exit 0; `--strict` gives exit 1 |
| `propose.bats` | base | manifest against golden, including the dragged-in chart |
| | no merged pull requests since the last release | empty manifest, exit 0 |
| `reconcile.bats` | unapproved | comment rendered, `actions` empty |
| | approved, no `release/go` | same. The gate is tested twice deliberately |
| | approved and labelled | tier 1 writes planned, chart absent from them |
| | `blocked-chart` | chart reads `blocked`, no chart write planned |
| `table.bats` | base | `--check` against golden, timestamp stripped |
| | run twice | second injection is a no-op, exit 0 |
| `variables.bats` | base | exit 0, `0 disagreement(s)` |
| | `drift` overlay | exit 1, names the component and both values |
| | 403 on the variables endpoint | exit 0 and `could not read`, so a fork does not fail CI |
| `collect-state.bats` | base | `state.json` against golden |
| `fork-obs.bats` | base | dry run lists planned forks, projects and links; writes nothing |
| | 401 from the build service | the refusal message, exit 1 |

### Determinism

Goldens are only usable if output is stable. Three things to establish
while writing them: the status table carries a timestamp, for which
`without_timestamp` already exists; dictionary iteration is sorted
before rendering; and no test reaches the real network, which the
server's request log makes visible as a fixture miss rather than a pass
that quietly depends on GitHub being up.

## Production changes

Two, both forced by the black-box requirement.

**GHCR needs an address, not only a host.** `common.py:855` and `:864`
build URLs as `https://{config.ghcr_registry}`, and the same value is
also the `service=` query parameter, which must stay a bare host. So
the fix is a sibling key rather than an edit to the existing one:

```yaml
  ghcr:
    registry: ghcr.io          # stays bare; it is the `service` parameter
    api: https://ghcr.io       # what the two URLs are built from
```

`load_config` defaults `api` to `https://{registry}` when absent, so
the fallback is exactly the current behaviour.

**`github_token()` must be silenceable.** It falls back to `gh auth
token` (`common.py:474`), so on a developer machine the end-to-end
suite sends a real token to the stub server while CI sends none. The
tests that matter most here — fork behaviour, and the 403-unreadable
variables case — would pass in one place and fail in the other.

Fix: presence beats the CLI, so `GITHUB_TOKEN` set but empty means "no
token, do not ask `gh`". Two lines. It changes nothing in CI, where the
variable already renders empty and `gh` is absent, so both paths
already yield `None`.

**`render_table.py` cannot be pointed at a configuration.** It is the
one script with no `--components` flag: `main` calls `load_config()`
with the default path, and `collect()` (`render_table.py:41`) spawns
`collect_state.py` as a subprocess without forwarding one. The status
table is in the end-to-end set, so it needs the flag and needs to pass
it to its child.

**Configuration, not behaviour:** a `pyproject.toml` carrying pytest
settings and ruff, and a `requirements-dev.txt`.

## CI

One new workflow, `.github/workflows/release-tests.yaml`, on pull
requests touching `scripts/release/**`, `release/**` or itself, and on
pushes to `main`.

Two jobs, both `ubuntu-24.04`, actions pinned by commit SHA with a
version comment, matching the house style:

- `unit` — venv, install `requirements-dev.txt`, run pytest. Seconds.
- `e2e` — the same venv plus `bats-core/bats-action`, then `bats
  scripts/release/tests/e2e/*.bats`. The stub server is stdlib, so
  nothing further installs.

`BATS_VERSION` goes in workflow `env:` with a link to the releases
page, as `helm-charts/.github/workflows/ci.yaml` does.

No coverage threshold. A percentage gate on a codebase going from zero
to covered mostly measures how much untested plumbing remains, and
would reward testing `render_human` over testing the reconciler gate.

## Out of scope

The private `trento-release` repository. Its 598 lines need the VPN to
exercise meaningfully, and `render_checklist.py` is the thinner half of
the two. The same pattern applies there if it is wanted later.

## Success criteria

- `pytest` and `bats` both pass with no network access
- the two fixed label bugs have a failing test if reintroduced
- `replace_yaml_value` has a test that fails if it is replaced with a
  ruamel round-trip
- the reconciler's approval gate has a test asserting an empty
  `performed` list
- `record.sh` regenerates every recorded fixture in one command
