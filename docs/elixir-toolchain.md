<!--
SPDX-FileCopyrightText: SUSE LLC
SPDX-License-Identifier: Apache-2.0
-->

# The OTP/Elixir toolchain actions

Two composite actions set up the Elixir CI jobs of a repository:

- `actions/elixir-setup` reads the Erlang/OTP and Elixir versions from the
  `.tool-versions` file of the caller repository. Then it builds and caches
  the dependencies for each toolchain and `MIX_ENV`, one after another.
- `actions/setup-elixir` installs Erlang/OTP and Elixir and restores the
  dependency cache in the other jobs.

## Outputs of `elixir-setup`

| | |
| --- | --- |
| `ELIXIR_DEV` | the Elixir version from `.tool-versions` |
| `ERLANG_DEV` | the Erlang/OTP version from `.tool-versions` |
| `TOOLCHAINS` | a JSON array of `{elixir, otp}` objects, for use as a matrix dimension |

By default, `TOOLCHAINS` holds one toolchain: the one from `.tool-versions`.

## Usage

A matrix needs the output of an earlier job. Run `elixir-setup` in its own
job, and expose its outputs as job outputs.

```yaml
jobs:
  elixir-setup:
    name: Elixir toolchain and dependencies
    runs-on: ubuntu-24.04
    permissions:
      contents: read
    outputs:
      TOOLCHAINS: ${{ steps.setup.outputs.TOOLCHAINS }}
    steps:
      - uses: actions/checkout@v7
        with:
          persist-credentials: false
      - id: setup
        uses: trento-project/.github/actions/elixir-setup@main
        with:
          mix-envs: '["dev","test"]'

  test:
    needs: [elixir-setup]
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        toolchain: ${{ fromJson(needs.elixir-setup.outputs.TOOLCHAINS) }}
    env:
      MIX_ENV: test
    steps:
      - uses: actions/checkout@v7
      - uses: trento-project/.github/actions/setup-elixir@main
        with:
          otp-version: ${{ matrix.toolchain.otp }}
          elixir-version: ${{ matrix.toolchain.elixir }}
```

The dependency build runs `mix deps.compile` with third-party code. The
`contents: read` permission and `persist-credentials: false` keep a write
token out of reach of that code.

To build the dependencies of another ref, for example the target branch of
a pull request, set `ref` in the checkout step. Also set the `toolchains`
input to the `TOOLCHAINS` output of the first job. The jobs that restore
these caches use the toolchains of the current ref.

## Limits

A composite action cannot loop over steps. `elixir-setup` has fixed steps
for 2 toolchains and 3 `MIX_ENV` values, and it fails on more. The builds
run one after another in one job. Before each build, the action removes
`deps`, `_build` and `priv/plts`, and it saves each cache right after its
build.

## Enable backward compatibility runs

Backward compatibility runs are disabled by default. To also test against
an older toolchain, add these inputs to the `elixir-setup` step:

```yaml
- id: setup
  uses: trento-project/.github/actions/elixir-setup@main
  with:
    mix-envs: '["dev","test"]'
    bc-enabled: "true"
    elixir-bc: "1.15.7-otp-26"
    erlang-bc: "26.2.1"
```

`TOOLCHAINS` then holds two toolchains: the backward compatibility
toolchain first, then the one from `.tool-versions`. Every job that uses
`TOOLCHAINS` as a matrix dimension runs once for each toolchain. You do not
change these jobs. `elixir-setup` also builds the dependencies for the older
toolchain.

If `bc-enabled` is `"true"` and `elixir-bc` or `erlang-bc` is empty, the
step fails.

To disable the runs again, remove these inputs.

## Releases and pinning

Callers pin the actions of this repository to the full commit SHA of a
release, with the tag as a comment. The examples above use `@main` to stay
short.

```yaml
uses: trento-project/.github/actions/setup-elixir@<sha> # v1.12.0
```

Pin all the actions to the same release. `elixir-setup` and `setup-elixir`
then compute the same cache key. A change to an action takes one release.

Each action keeps its bash logic in a script next to `action.yaml`, with
bats tests in `tests/`. Run them with `bats actions/*/tests/*.bats`.
