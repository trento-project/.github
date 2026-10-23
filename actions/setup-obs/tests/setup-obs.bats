#!/usr/bin/env bats

# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: GPL-3.0-or-later

# The script runs in the continuous-delivery image, which provides
# init_osc_creds.sh. These tests run it there with Docker, as root and with
# HOME=/github/home, as in the OBS sync jobs. Docker removes the container
# after each test, so the files that the script writes do not stay.

setup_file() {
  command -v docker > /dev/null || skip "Docker is not available"
  local workflow
  workflow="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../.." && pwd)/.github/workflows/obs-sync.yaml"
  IMAGE=$(grep -m1 -oE 'ghcr\.io/trento-project/continuous-delivery@sha256:[0-9a-f]{64}' "$workflow")
  export IMAGE
  docker pull -q --platform linux/amd64 "$IMAGE" > /dev/null
}

setup() {
  ACTION_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
}

# setup_obs [DOCKER OPTION...]: run the script in the image with test
# credentials. Then print each file it wrote after a "== PATH" line, the mode
# of the key file, and the Git identity.
setup_obs() {
  run docker run --rm --platform linux/amd64 -u 0:0 \
    -v "$ACTION_DIR:/action:ro" \
    -e HOME=/github/home -e OBS_USER=bot -e OBS_PASS=secret \
    -e OBS_SSH_KEY=test-key -e AUTHOR_EMAIL=bot@example.com \
    "$@" "$IMAGE" bash -c '
      mkdir -p "$HOME"
      /action/setup-obs.sh || exit
      for file in /root/.ssh/config /root/.ssh/known_hosts /root/.ssh/id_ed25519_obs \
        "$HOME/.config/osc/oscrc"; do
        echo "== $file"
        cat "$file"
      done
      echo "== mode $(stat -c %a /root/.ssh/id_ed25519_obs)"
      echo "== git $(git config --global user.name) $(git config --global user.email)"'
}

# file PATH: the content of PATH in the output of setup_obs.
file() {
  awk -v path="== $1" '$0 == path { on = 1; next } /^== / { on = 0 } on' <<< "$output"
}

@test "osc logs in with the password, not with the SSH key" {
  setup_obs

  [ "$status" -eq 0 ]
  oscrc=$(file /github/home/.config/osc/oscrc)
  [[ "$oscrc" == *"user = bot"* ]]
  [[ "$oscrc" == *"pass = secret"* ]]
  ! grep -qE '^(sshkey|credentials_mgr_class)=' <<< "$oscrc"
}

@test "Git on src.opensuse.org uses the SSH key, from the home directory in /etc/passwd" {
  setup_obs

  [ "$status" -eq 0 ]
  [ "$(file /root/.ssh/id_ed25519_obs)" = test-key ]
  [[ "$output" == *"== mode 600"* ]]
  [[ "$(file /root/.ssh/config)" == *$'Host src.opensuse.org\n  IdentityFile /root/.ssh/id_ed25519_obs\n  IdentitiesOnly yes'* ]]
  [[ "$(file /root/.ssh/known_hosts)" == "src.opensuse.org ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFKNThLRPznU5Io1KrAYHmYpaoLQEMGM9nwpKyYQCkPx" ]]
}

@test "the commits use the OBS user and the author email" {
  setup_obs

  [ "$status" -eq 0 ]
  [[ "$output" == *"== git bot bot@example.com"* ]]
}

@test "stops when the SSH key is missing, instead of writing an empty key" {
  setup_obs -e OBS_SSH_KEY=

  [ "$status" -ne 0 ]
  [[ "$output" == *"OBS_SSH_KEY"* ]]
}
