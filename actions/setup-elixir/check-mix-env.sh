#!/usr/bin/env bash

# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

if [[ -z "${MIX_ENV:-}" ]]; then
  echo "::error::MIX_ENV must be set in the job environment"
  exit 1
fi
