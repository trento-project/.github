#!/usr/bin/env bash

# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

if [[ -z "${ELIXIR_VERSION:-}" || -z "${ERLANG_VERSION:-}" ]]; then
  echo "::error::.tool-versions must define both elixir and erlang"
  exit 1
fi
if [[ "${BC_ENABLED:-}" == "true" && ( -z "${ELIXIR_BC:-}" || -z "${ERLANG_BC:-}" ) ]]; then
  echo "::error::bc_enabled requires elixir_bc and erlang_bc"
  exit 1
fi
TOOLCHAINS=$(jq -cn \
  --arg elixir_bc "${ELIXIR_BC:-}" --arg otp_bc "${ERLANG_BC:-}" \
  --arg elixir_dev "$ELIXIR_VERSION" --arg otp_dev "$ERLANG_VERSION" \
  --arg bc_enabled "${BC_ENABLED:-}" '
  [
    {elixir: $elixir_bc,  otp: $otp_bc},
    {elixir: $elixir_dev, otp: $otp_dev}
  ]
  | if $bc_enabled == "true" then . else .[1:] end')
{
  echo "ELIXIR_DEV=$ELIXIR_VERSION"
  echo "ERLANG_DEV=$ERLANG_VERSION"
  echo "TOOLCHAINS=$TOOLCHAINS"
} >> "$GITHUB_OUTPUT"
