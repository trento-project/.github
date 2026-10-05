#!/usr/bin/env bats

# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

# This action writes the caches that actions/setup-elixir restores. Each of
# the 6 slots has a restore key and a save key, and all of them must match
# the setup-elixir key format.

setup() {
  ACTION="${ACTION:-$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/action.yaml}"
}

key() {
  echo "key: erlang-\${{ steps.matrix.outputs.T$1_OTP }}-elixir-\${{ steps.matrix.outputs.T$1_ELIXIR }}\${{ steps.matrix.outputs.SUFFIX }}-\${{ hashFiles('mix.lock') }}-\${{ steps.matrix.outputs.ENV$2 }}"
}

@test "cache keys: one restore and one save per slot, setup-elixir format" {
  run grep -E '^ +key: ' "$ACTION"
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 12 ]
  expected=""
  for t in 0 1; do
    for e in 0 1 2; do
      expected+="$(key "$t" "$e")"$'\n'"$(key "$t" "$e")"$'\n'
    done
  done
  [ "$(printf '%s\n' "${lines[@]}" | sed -E 's/^ +//')"$'\n' = "$expected" ]
}
