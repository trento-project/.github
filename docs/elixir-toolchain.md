<!--
SPDX-FileCopyrightText: SUSE LLC
SPDX-License-Identifier: Apache-2.0
-->

# The OTP/Elixir toolchain actions

Two composite actions set up the Elixir CI jobs of a repository:

- `actions/elixir-toolchain` tells the jobs which Erlang/OTP and Elixir
  versions to use. It reads them from the `.tool-versions` file of the
  caller repository.
- `actions/setup-elixir` installs Erlang/OTP and Elixir and restores the
  dependency cache. With `build-deps: "true"`, it also builds the
  dependencies on a cache miss.

## Toolchain outputs

| | |
| --- | --- |
| `ELIXIR_DEV` | the Elixir version from `.tool-versions` |
| `ERLANG_DEV` | the Erlang/OTP version from `.tool-versions` |
| `TOOLCHAINS` | a JSON array of `{elixir, otp}` objects, for use as a matrix dimension |

By default, `TOOLCHAINS` holds one toolchain: the one from `.tool-versions`.

## Usage

A matrix needs the output of an earlier job. Run the toolchain action in
its own job, and expose its outputs as job outputs. Build the dependencies
in one job, then use them in the other jobs.

```yaml
jobs:
  elixir-toolchain:
    name: Elixir toolchain
    runs-on: ubuntu-24.04
    permissions:
      contents: read
    outputs:
      TOOLCHAINS: ${{ steps.toolchain.outputs.TOOLCHAINS }}
    steps:
      - uses: actions/checkout@v7
        with:
          persist-credentials: false
      - id: toolchain
        uses: trento-project/.github/actions/elixir-toolchain@main

  elixir-deps:
    name: Elixir ${{ matrix.mix_env }} dependencies (Elixir ${{ matrix.toolchain.elixir }})
    needs: [elixir-toolchain]
    runs-on: ubuntu-24.04
    permissions:
      contents: read
    strategy:
      fail-fast: false
      matrix:
        mix_env: [dev, test]
        toolchain: ${{ fromJson(needs.elixir-toolchain.outputs.TOOLCHAINS) }}
    env:
      MIX_ENV: ${{ matrix.mix_env }}
    steps:
      - uses: actions/checkout@v7
        with:
          persist-credentials: false
      - uses: trento-project/.github/actions/setup-elixir@main
        with:
          otp-version: ${{ matrix.toolchain.otp }}
          elixir-version: ${{ matrix.toolchain.elixir }}
          build-deps: "true"

  test:
    needs: [elixir-toolchain, elixir-deps]
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

The dependency build runs `mix deps.compile` with third-party code. The
`contents: read` permission and `persist-credentials: false` keep a write
token out of reach of that code.

To build the dependencies of another ref, for example the target branch
of a pull request, set `ref` in the checkout step.

## Enable backward compatibility runs

Backward compatibility runs are disabled by default. To also test against
an older toolchain, add these inputs to the toolchain step:

```yaml
- id: toolchain
  uses: trento-project/.github/actions/elixir-toolchain@main
  with:
    bc-enabled: "true"
    elixir-bc: "1.15.7-otp-26"
    erlang-bc: "26.2.1"
```

`TOOLCHAINS` then holds two toolchains: the backward compatibility
toolchain first, then the one from `.tool-versions`. Every job that uses
`TOOLCHAINS` as a matrix dimension runs once for each toolchain. You do not
change these jobs. This includes the dependency build, so the cache for the
older toolchain is built too.

If `bc-enabled` is `"true"` and `elixir-bc` or `erlang-bc` is empty, the
step fails.

To disable the runs again, remove the `with:` block.

## Releases and pinning

Callers pin the actions of this repository to the full commit SHA of a
release, with the tag as a comment. The examples above use `@main` to stay
short.

```yaml
uses: trento-project/.github/actions/setup-elixir@<sha> # v1.12.0
```

Pin all the actions to the same release. The dependency build and the
other jobs then use the same `setup-elixir`, so they compute the same cache
key. A change to an action takes one release.

Each action keeps its bash logic in a script next to `action.yaml`, with
bats tests in `tests/`. Run them with `bats actions/*/tests/*.bats`.
