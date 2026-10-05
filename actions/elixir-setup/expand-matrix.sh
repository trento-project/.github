#!/usr/bin/env bash

# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

# A composite action cannot loop over steps. The action has fixed slots for
# 2 toolchains and 3 MIX_ENV values. This script fills the slots.

set -euo pipefail

TOOLCHAINS=$(jq -c . <<< "$TOOLCHAINS")
MIX_ENVS=$(jq -c . <<< "$MIX_ENVS")

if ! jq -e 'length >= 1 and length <= 2' <<< "$TOOLCHAINS" > /dev/null; then
  echo "::error::the action supports 1 or 2 toolchains"
  exit 1
fi
if ! jq -e 'length >= 1 and length <= 3' <<< "$MIX_ENVS" > /dev/null; then
  echo "::error::mix-envs must hold 1 to 3 values"
  exit 1
fi

{
  echo "TOOLCHAINS=$TOOLCHAINS"
  jq -r 'to_entries[] | "T\(.key)_ELIXIR=\(.value.elixir)", "T\(.key)_OTP=\(.value.otp)"' <<< "$TOOLCHAINS"
  [[ $(jq length <<< "$TOOLCHAINS") -eq 2 ]] || printf 'T1_ELIXIR=\nT1_OTP=\n'
  jq -r '[.[0], .[1], .[2]] | to_entries[] | "ENV\(.key)=\(.value // "")"' <<< "$MIX_ENVS"
  echo "SUFFIX=${CACHE_KEY_SUFFIX:+-$CACHE_KEY_SUFFIX}"
} >> "$GITHUB_OUTPUT"
