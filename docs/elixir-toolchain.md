<!--
SPDX-FileCopyrightText: SUSE LLC
SPDX-License-Identifier: Apache-2.0
-->

# The OTP/Elixir toolchain workflow

`.github/workflows/elixir-toolchain.yaml` tells the Elixir CI jobs of a
repository which Erlang/OTP and Elixir versions to use. It reads them from
the `.tool-versions` file of the caller repository.

## Outputs

| | |
| --- | --- |
| `ELIXIR_DEV` | the Elixir version from `.tool-versions` |
| `ERLANG_DEV` | the Erlang/OTP version from `.tool-versions` |
| `TOOLCHAINS` | a JSON array of `{elixir, otp}` objects, for use as a matrix dimension |

By default, `TOOLCHAINS` holds one toolchain: the one from `.tool-versions`.

## Usage

```yaml
jobs:
  elixir-toolchain:
    name: Elixir toolchain
    uses: trento-project/.github/.github/workflows/elixir-toolchain.yaml@main

  test:
    needs: [elixir-toolchain]
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        toolchain: ${{ fromJson(needs.elixir-toolchain.outputs.TOOLCHAINS) }}
    env:
      MIX_ENV: test
    steps:
      - uses: actions/checkout@v7
      - uses: trento-project/.github/actions/setup-elixir@main
        with:
          otp-version: ${{ matrix.toolchain.otp }}
          elixir-version: ${{ matrix.toolchain.elixir }}
```

To read `.tool-versions` from another ref, for example the target branch
of a pull request, set `checkout_ref`.

## Enable backward compatibility runs

Backward compatibility runs are disabled by default. To also test against
an older toolchain, add these inputs to the `elixir-toolchain` job:

```yaml
jobs:
  elixir-toolchain:
    name: Elixir toolchain
    uses: trento-project/.github/.github/workflows/elixir-toolchain.yaml@main
    with:
      bc_enabled: true
      elixir_bc: "1.15.7-otp-26"
      erlang_bc: "26.2.1"
```

`TOOLCHAINS` then holds two toolchains: the backward compatibility
toolchain first, then the one from `.tool-versions`. Every job that uses
`TOOLCHAINS` as a matrix dimension runs once for each toolchain. You do not
change these jobs. This includes the dependency build in
`elixir-deps.yaml`, so the cache for the older toolchain is built too.

If `bc_enabled` is true and `elixir_bc` or `erlang_bc` is empty, the
workflow fails.

To disable the runs again, remove the `with:` block.

## Releases and pinning

Callers pin the workflows and actions of this repository to the full
commit SHA of a release, with the tag as a comment. The examples above use
`@main` to stay short.

```yaml
uses: trento-project/.github/.github/workflows/elixir-deps.yaml@<sha> # v1.12.0
```

Pin `elixir-toolchain.yaml`, `elixir-deps.yaml` and the actions in
`actions/` to the same release.

### Change an action in `actions/`

The workflows use the actions of this repository with a full reference,
because `./` resolves against the caller repository:

| Workflow | Actions |
|---|---|
| `elixir-toolchain.yaml` | `actions/elixir-toolchain` |
| `elixir-deps.yaml` | `actions/export-env`, `actions/setup-elixir` |

A commit cannot contain its own SHA, so a change to an action takes two
releases. Until the first release is pinned, the workflows use the actions
at `@main`.

1. Merge the change to `main`.
2. Create a release, for example `v1.11.0`.
3. Get the commit SHA of the release with `git rev-list -n 1 v1.11.0`.
4. In a new pull request, pin all the action references in both workflows to that SHA.
5. Merge the pull request and create a release, for example `v1.12.0`.
6. Pin the callers to `v1.12.0`.

Dependabot in this repository proposes new pins for the workflows after
each release. Merge them only if an action changed after the pinned
release.

Each action keeps its bash logic in a script next to `action.yaml`, with
bats tests in `tests/`. Run them with `bats actions/*/tests/*.bats`.

### Check the cache key before you pin callers

The dependency build in `elixir-deps.yaml` writes the cache with the
`actions/setup-elixir` that it pins. The other caller jobs read the cache
with the `actions/setup-elixir` of the release that the caller pins. If
the two compute different cache keys, the caller jobs find no
dependencies and fail.

Before you pin callers to a release, make sure that this command shows no
difference:

```shell
git diff <sha pinned in elixir-deps.yaml> <release tag> -- actions/setup-elixir
```
