#!/usr/bin/env bats

# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

setup() {
  SCRIPT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/compute-toolchain.sh"
  export GITHUB_OUTPUT="$BATS_TEST_TMPDIR/github_output"
  : > "$GITHUB_OUTPUT"
  unset ELIXIR_BC ERLANG_BC BC_ENABLED
  export ELIXIR_VERSION=1.19.5-otp-27
  export ERLANG_VERSION=27.3.4.11
}

DEV='{"elixir":"1.19.5-otp-27","otp":"27.3.4.11"}'
BC='{"elixir":"1.15.7-otp-26","otp":"26.2.1"}'

set_bc() {
  export ELIXIR_BC=1.15.7-otp-26
  export ERLANG_BC=26.2.1
}

@test "script is executable" {
  [ -x "$SCRIPT" ]
}

@test "no BC variables set: dev toolchain only" {
  run "$SCRIPT"
  [ "$status" -eq 0 ]
  [ "$(cat "$GITHUB_OUTPUT")" = "ELIXIR_DEV=1.19.5-otp-27
ERLANG_DEV=27.3.4.11
TOOLCHAINS=[$DEV]" ]
}

@test "BC enabled: BC toolchain first, then dev" {
  set_bc
  export BC_ENABLED=true
  run "$SCRIPT"
  [ "$status" -eq 0 ]
  grep -qxF "TOOLCHAINS=[$BC,$DEV]" "$GITHUB_OUTPUT"
}

@test "BC versions set, BC disabled: dev toolchain only" {
  set_bc
  export BC_ENABLED=false
  run "$SCRIPT"
  [ "$status" -eq 0 ]
  grep -qxF "TOOLCHAINS=[$DEV]" "$GITHUB_OUTPUT"
}

@test "BC enabled without elixir_bc: fails" {
  set_bc
  unset ELIXIR_BC
  export BC_ENABLED=true
  run "$SCRIPT"
  [ "$status" -eq 1 ]
  [ "$output" = "::error::bc_enabled requires elixir_bc and erlang_bc" ]
  [ ! -s "$GITHUB_OUTPUT" ]
}

@test "BC enabled without erlang_bc: fails" {
  set_bc
  export ERLANG_BC=""
  export BC_ENABLED=true
  run "$SCRIPT"
  [ "$status" -eq 1 ]
  [ "$output" = "::error::bc_enabled requires elixir_bc and erlang_bc" ]
}

@test "no elixir in .tool-versions: fails" {
  unset ELIXIR_VERSION
  run "$SCRIPT"
  [ "$status" -eq 1 ]
  [ "$output" = "::error::.tool-versions must define both elixir and erlang" ]
}

@test "no erlang in .tool-versions: fails" {
  export ERLANG_VERSION=""
  run "$SCRIPT"
  [ "$status" -eq 1 ]
  [ "$output" = "::error::.tool-versions must define both elixir and erlang" ]
}
