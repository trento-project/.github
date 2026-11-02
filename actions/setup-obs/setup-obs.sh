#!/usr/bin/env bash

# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: GPL-3.0-or-later

# Give the job access to OBS: SSH for Git on src.opensuse.org, the osc
# credentials, and the Git identity for the commits.

set -euo pipefail

: "${OBS_USER:?}" "${OBS_PASS:?}" "${OBS_SSH_KEY:?}" "${AUTHOR_EMAIL:?}"

# SSH is only used for Git, not for osc. GitHub changes $HOME, but SSH
# expands ~ from /etc/passwd, so write to the home directory from there.
real_home=$(getent passwd "$(whoami)" | cut -d':' -f6)
mkdir -p "$real_home/.ssh"
chmod 700 "$real_home/.ssh"

host_key=AAAAC3NzaC1lZDI1NTE5AAAAIFKNThLRPznU5Io1KrAYHmYpaoLQEMGM9nwpKyYQCkPx
echo "src.opensuse.org ssh-ed25519 $host_key" >> "$real_home/.ssh/known_hosts"

ident_file="$real_home/.ssh/id_ed25519_obs"
echo "$OBS_SSH_KEY" > "$ident_file"
chmod 600 "$ident_file"

cat << EOF >> "$real_home/.ssh/config"
Host src.opensuse.org
  IdentityFile $ident_file
  IdentitiesOnly yes
EOF

# With OBS_SSH_KEY set, init_osc_creds.sh switches osc to a login with that
# key. The key is for Git on src.opensuse.org, so keep the password login.
env -u OBS_SSH_KEY /scripts/init_osc_creds.sh

git config --global user.name "$OBS_USER"
git config --global user.email "$AUTHOR_EMAIL"
