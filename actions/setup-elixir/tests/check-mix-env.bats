#!/usr/bin/env bats

# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

setup() {
  SCRIPT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/check-mix-env.sh"
  unset MIX_ENV
}

@test "script is executable" {
  [ -x "$SCRIPT" ]
}

@test "MIX_ENV set: succeeds" {
  export MIX_ENV=test
  run "$SCRIPT"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "MIX_ENV unset: fails" {
  run "$SCRIPT"
  [ "$status" -eq 1 ]
  [ "$output" = "::error::MIX_ENV must be set in the job environment" ]
}

@test "MIX_ENV empty: fails" {
  export MIX_ENV=""
  run "$SCRIPT"
  [ "$status" -eq 1 ]
}
