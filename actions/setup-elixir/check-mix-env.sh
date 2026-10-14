#!/usr/bin/env bash

# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: GPL-3.0-or-later

set -euo pipefail

if [[ -z "${MIX_ENV:-}" ]]; then
  echo "::error::MIX_ENV must be set in the job environment"
  exit 1
fi
