#!/usr/bin/env bats

# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

# The jobs that build the dependencies and the jobs that restore them can pin
# different releases of this action. A change to the key makes every restore
# miss, so the key must change only on purpose.

setup() {
  ACTION="${ACTION:-$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/action.yaml}"
}

@test "cache key: unchanged format" {
  run grep -E '^ +key: ' "$ACTION"
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 1 ]
  expected="key: erlang-\${{ inputs.otp-version }}-elixir-\${{ inputs.elixir-version }}\${{ inputs.cache-key-suffix != '' && format('-{0}', inputs.cache-key-suffix) || '' }}-\${{ hashFiles('mix.lock') }}-\${{ env.MIX_ENV }}"
  [ "$(echo "${lines[0]}" | sed -E 's/^ +//')" = "$expected" ]
}
