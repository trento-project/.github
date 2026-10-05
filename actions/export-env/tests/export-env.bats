#!/usr/bin/env bats

# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

setup() {
  SCRIPT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/export-env.sh"
  export GITHUB_ENV="$BATS_TEST_TMPDIR/github_env"
  : > "$GITHUB_ENV"
}

export_env() {
  run env EXTRA_ENV="$1" "$SCRIPT"
}

assert_rejected() {
  [ "$status" -ne 0 ]
  [[ "$output" == *"$1"* ]]
  [ ! -s "$GITHUB_ENV" ]
}

@test "script is executable" {
  [ -x "$SCRIPT" ]
}

@test "object: one line per key" {
  export_env '{"A": "1", "B_2": "x y"}'
  [ "$status" -eq 0 ]
  [ "$(cat "$GITHUB_ENV")" = $'A=1\nB_2=x y' ]
}

@test "empty object: writes nothing" {
  export_env '{}'
  [ "$status" -eq 0 ]
  [ ! -s "$GITHUB_ENV" ]
}

@test "array: rejected" {
  export_env '["A"]'
  assert_rejected "env must be a JSON object"
}

@test "invalid JSON: rejected" {
  export_env '{"A": '
  assert_rejected "parse error"
}

@test "newline in value: rejected" {
  export_env '{"A": "x\ny"}'
  assert_rejected "env values must be single-line"
}

@test "carriage return in value: rejected" {
  export_env '{"A": "x\ry"}'
  assert_rejected "env values must be single-line"
}

@test "MIX_ENV key: rejected" {
  export_env '{"MIX_ENV": "prod"}'
  assert_rejected "env must not set MIX_ENV, use mix_envs"
}

@test "number and boolean values: written as text" {
  export_env '{"N": 1, "B": true}'
  [ "$status" -eq 0 ]
  [ "$(cat "$GITHUB_ENV")" = $'N=1\nB=true' ]
}

@test "key with '=': rejected" {
  export_env '{"A=B": "x"}'
  assert_rejected "env keys must be valid environment variable names"
}

@test "empty key: rejected" {
  export_env '{"": "x"}'
  assert_rejected "env keys must be valid environment variable names"
}

@test "key with leading digit: rejected" {
  export_env '{"1ABC": "x"}'
  assert_rejected "env keys must be valid environment variable names"
}

@test "'=' inside a value: kept" {
  export_env '{"A": "b=c"}'
  [ "$status" -eq 0 ]
  [ "$(cat "$GITHUB_ENV")" = "A=b=c" ]
}

@test "null value: rejected" {
  export_env '{"A": null}'
  assert_rejected "env values must be strings, numbers or booleans"
}

@test "object value: rejected" {
  export_env '{"A": {"B": "x"}}'
  assert_rejected "env values must be strings, numbers or booleans"
}

@test "array value: rejected" {
  export_env '{"A": ["x"]}'
  assert_rejected "env values must be strings, numbers or booleans"
}
