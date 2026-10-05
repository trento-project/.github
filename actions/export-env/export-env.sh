#!/usr/bin/env bash

# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

jq -r '
  if type != "object" then error("env must be a JSON object")
  elif has("MIX_ENV") then error("env must not set MIX_ENV, use mix_envs")
  elif any(keys[]; test("^[A-Za-z_][A-Za-z0-9_]*$") | not) then error("env keys must be valid environment variable names")
  elif any(.[]; tostring | test("[\r\n]")) then error("env values must be single-line")
  else to_entries[] | "\(.key)=\(.value)"
  end' <<< "${EXTRA_ENV:-}" >> "$GITHUB_ENV"
