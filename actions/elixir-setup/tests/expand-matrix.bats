#!/usr/bin/env bats

# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

setup() {
  SCRIPT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/expand-matrix.sh"
  export GITHUB_OUTPUT="$BATS_TEST_TMPDIR/github_output"
  : > "$GITHUB_OUTPUT"
  unset CACHE_KEY_SUFFIX
  export TOOLCHAINS='[{"elixir":"1.19.5-otp-27","otp":"27.3.4.11"}]'
  export MIX_ENVS='["test"]'
}

DEV='{"elixir":"1.19.5-otp-27","otp":"27.3.4.11"}'
BC='{"elixir":"1.15.7-otp-26","otp":"26.2.1"}'

@test "script is executable" {
  [ -x "$SCRIPT" ]
}

@test "one toolchain, one env: unused slots are empty" {
  run "$SCRIPT"
  [ "$status" -eq 0 ]
  [ "$(cat "$GITHUB_OUTPUT")" = "TOOLCHAINS=[$DEV]
T0_ELIXIR=1.19.5-otp-27
T0_OTP=27.3.4.11
T1_ELIXIR=
T1_OTP=
ENV0=test
ENV1=
ENV2=
SUFFIX=" ]
}

@test "two toolchains, three envs: all slots set" {
  export TOOLCHAINS="[$BC, $DEV]"
  export MIX_ENVS='["dev","test","prod"]'
  run "$SCRIPT"
  [ "$status" -eq 0 ]
  grep -qxF "TOOLCHAINS=[$BC,$DEV]" "$GITHUB_OUTPUT"
  grep -qxF "T0_ELIXIR=1.15.7-otp-26" "$GITHUB_OUTPUT"
  grep -qxF "T1_OTP=27.3.4.11" "$GITHUB_OUTPUT"
  grep -qxF "ENV2=prod" "$GITHUB_OUTPUT"
}

@test "cache key suffix: prefixed with a dash" {
  export CACHE_KEY_SUFFIX=rust-1.92
  run "$SCRIPT"
  [ "$status" -eq 0 ]
  grep -qxF "SUFFIX=-rust-1.92" "$GITHUB_OUTPUT"
}

@test "three toolchains: fails" {
  export TOOLCHAINS="[$BC, $BC, $DEV]"
  run "$SCRIPT"
  [ "$status" -eq 1 ]
  [ "$output" = "::error::the action supports 1 or 2 toolchains" ]
  [ ! -s "$GITHUB_OUTPUT" ]
}

@test "no toolchain: fails" {
  export TOOLCHAINS='[]'
  run "$SCRIPT"
  [ "$status" -eq 1 ]
  [ "$output" = "::error::the action supports 1 or 2 toolchains" ]
}

@test "four envs: fails" {
  export MIX_ENVS='["dev","test","prod","bench"]'
  run "$SCRIPT"
  [ "$status" -eq 1 ]
  [ "$output" = "::error::mix-envs must hold 1 to 3 values" ]
  [ ! -s "$GITHUB_OUTPUT" ]
}

@test "invalid JSON: fails" {
  export MIX_ENVS='["dev"'
  run "$SCRIPT"
  [ "$status" -ne 0 ]
  [ ! -s "$GITHUB_OUTPUT" ]
}
