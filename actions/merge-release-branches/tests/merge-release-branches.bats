#!/usr/bin/env bats

# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: GPL-3.0-or-later

# Each test has a bare "origin" repository and a "dev" clone that stands in
# for the developers. checkout clones origin the way the release jobs do, with
# signing turned on the way crazy-max/ghaction-import-gpg does it.

setup_file() {
  export GNUPGHOME="$BATS_FILE_TMPDIR/gnupg"
  mkdir -m 700 "$GNUPGHOME"
  gpg --batch --quiet --passphrase '' --quick-gen-key 'bot <bot@example.com>' ed25519 sign never 2> /dev/null
}

teardown_file() {
  gpgconf --kill gpg-agent
}

setup() {
  SCRIPT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/merge-release-branches.sh"

  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
  export GIT_AUTHOR_NAME=bot GIT_AUTHOR_EMAIL=bot@example.com
  export GIT_COMMITTER_NAME=bot GIT_COMMITTER_EMAIL=bot@example.com

  ORIGIN="$BATS_TEST_TMPDIR/origin.git"
  DEV="$BATS_TEST_TMPDIR/dev"
  JOB="$BATS_TEST_TMPDIR/job"
  git init -q --bare -b main "$ORIGIN"
  git init -q -b main "$DEV"
  git -C "$DEV" remote add origin "$ORIGIN"
}

# commit BRANCH FILE CONTENT MESSAGE: on top of BRANCH in origin, write FILE
# with CONTENT, commit, and push to BRANCH.
commit() {
  git -C "$DEV" fetch -q origin
  if git -C "$DEV" rev-parse -q --verify "origin/$1" > /dev/null; then
    git -C "$DEV" switch -q -C "$1" "origin/$1"
  fi
  mkdir -p "$(dirname "$DEV/$2")"
  printf '%s\n' "$3" > "$DEV/$2"
  git -C "$DEV" add -A
  git -C "$DEV" commit -q -m "$4"
  git -C "$DEV" push -q origin "HEAD:refs/heads/$1"
}

# remove BRANCH FILE MESSAGE: on top of BRANCH in origin, delete FILE, commit,
# and push to BRANCH.
remove() {
  git -C "$DEV" fetch -q origin
  git -C "$DEV" switch -q -C "$1" "origin/$1"
  git -C "$DEV" --literal-pathspecs rm -q -- "$2"
  git -C "$DEV" commit -q -m "$3"
  git -C "$DEV" push -q origin "HEAD:refs/heads/$1"
}

# The lock file that most tests change. It recalls web 3.2.0, where a backport
# that existed only on release leaked into assets/package-lock.json of the
# release. Each line pins one package, with a blank line between packages, so
# that two branches can bump different packages without a conflict.
LOCK_FILE=assets/package-lock.txt

# add_lock BRANCH: commit the lock file of the last release on BRANCH.
add_lock() {
  commit "$1" "$LOCK_FILE" $'lodash@4.17.20\n\nreact@18.2.0\n\naxios@1.6.0' "Add the lock file"
}

# bump BRANCH MESSAGE PACKAGE@VERSION...: on top of BRANCH in origin, pin each
# PACKAGE at VERSION in the lock file, commit, and push to BRANCH.
bump() {
  local branch=$1 message=$2 line pin content=""
  shift 2
  for pin in "$@"; do
    git -C "$ORIGIN" show "$branch:$LOCK_FILE" | grep -q "^${pin%@*}@" ||
      { echo "bump: ${pin%@*} is not in the lock file" >&2; return 1; }
  done
  while IFS= read -r line; do
    for pin in "$@"; do
      [ "${line%@*}" = "${pin%@*}" ] && line=$pin
    done
    content+="$line"$'\n'
  done < <(git -C "$ORIGIN" show "$branch:$LOCK_FILE")
  commit "$branch" "$LOCK_FILE" "${content%$'\n'}" "$message"
}

# lock_at REF: the packages of the lock file at REF in origin, on one line.
lock_at() {
  git -C "$ORIGIN" show "$1:$LOCK_FILE" | grep . | paste -sd ' ' -
}

# copy_branch FROM TO: point TO in origin at the commit of FROM.
copy_branch() {
  git -C "$ORIGIN" update-ref "refs/heads/$2" "refs/heads/$1"
}

# rev REF: the commit of REF in origin.
rev() {
  git -C "$ORIGIN" rev-parse "$1^{commit}"
}

# tree REF: the tree of REF in origin.
tree() {
  git -C "$ORIGIN" rev-parse "$1^{tree}"
}

# show REF FILE: the content of FILE at REF in origin.
show() {
  git -C "$ORIGIN" show "$1:$2"
}

# origin_refs: every ref of origin with its object, to spot any push.
origin_refs() {
  git -C "$ORIGIN" for-each-ref --format='%(refname) %(objectname)'
}

# checkout TYPE: clone origin as the job of TYPE does. The full release job
# checks out main, and the hotfix job checks out release.
checkout() {
  local branch=main
  [ "$1" = hotfix ] && branch=release
  rm -rf "$JOB"
  git clone -q --branch "$branch" "$ORIGIN" "$JOB"
  git -C "$JOB" config user.signingkey bot@example.com
  git -C "$JOB" config commit.gpgsign true
  git -C "$JOB" config tag.gpgsign true
}

# run_script TYPE VERSION [NAME=VALUE...]: run the script in the job clone,
# with the extra environment NAME=VALUE...
run_script() {
  local type=$1 version=$2
  shift 2
  cd "$JOB"
  run env RELEASE_TYPE="$type" VERSION="$version" "$@" "$SCRIPT"
  cd "$BATS_TEST_TMPDIR"
}

# release TYPE VERSION [NAME=VALUE...]: checkout, then run_script.
release() {
  checkout "$1"
  run_script "$@"
}

# The git configuration in the environment that makes every signature fail.
NO_SIGNING=(GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=gpg.program GIT_CONFIG_VALUE_0=false)

# A full release

@test "full release: a backport only on release does not leak into the release" {
  add_lock main
  copy_branch main release
  bump release "Backport a fix" lodash@4.17.21 react@18.2.1
  bump main "Update react and axios on main" react@18.3.0 axios@1.7.0
  main_before=$(rev main)

  release full 1.1.0

  [ "$status" -eq 0 ]
  [ "$(lock_at 1.1.0)" = "lodash@4.17.20 react@18.3.0 axios@1.7.0" ]
  [ "$(tree 1.1.0)" = "$(tree "$main_before")" ]
}

@test "full release: main wins when both branches change the same line" {
  add_lock main
  copy_branch main release
  bump release "Backport a fix" react@18.2.1
  bump main "Update react on main" react@18.3.0

  release full 2.1.0

  [ "$status" -eq 0 ]
  [ "$(lock_at 2.1.0)" = "lodash@4.17.20 react@18.3.0 axios@1.6.0" ]
}

@test "full release: a file changed on release and deleted on main stays deleted" {
  commit main app/legacy.txt legacy "Add a legacy file"
  copy_branch main release
  commit release app/legacy.txt "legacy, fixed" "Backport a fix to the legacy file"
  remove main app/legacy.txt "Remove the legacy file"

  release full 3.1.0

  [ "$status" -eq 0 ]
  run git -C "$ORIGIN" cat-file -e 3.1.0:app/legacy.txt
  [ "$status" -ne 0 ]
}

@test "full release: the first release creates release from main, with no new commit" {
  add_lock main
  main_before=$(rev main)

  release full 4.1.0

  [ "$status" -eq 0 ]
  [ "$(rev 4.1.0)" = "$main_before" ]
  [ "$(rev main)" = "$main_before" ]
  [ "$(rev release)" = "$main_before" ]
}

@test "full release: release ahead of main keeps the tree of main" {
  add_lock main
  copy_branch main release
  bump release "Backport a fix" lodash@4.17.21
  main_before=$(rev main)

  release full 5.1.0

  [ "$status" -eq 0 ]
  [ "$(lock_at 5.1.0)" = "lodash@4.17.20 react@18.2.0 axios@1.6.0" ]
  [ "$(tree 5.1.0)" = "$(tree "$main_before")" ]
}

@test "full release: release already in main adds no commit, and release moves to main" {
  add_lock main
  copy_branch main release
  bump main "Update axios on main" axios@1.7.0
  main_before=$(rev main)

  release full 5.2.0

  [ "$status" -eq 0 ]
  [ "$(rev 5.2.0)" = "$main_before" ]
  [ "$(rev main)" = "$main_before" ]
  [ "$(rev release)" = "$main_before" ]
}

@test "full release: the release commit has main, then release, as parents" {
  add_lock main
  copy_branch main release
  bump release "Backport a fix" lodash@4.17.21
  bump main "Update axios on main" axios@1.7.0
  release_before=$(rev release)
  main_before=$(rev main)

  release full 5.3.0

  [ "$status" -eq 0 ]
  [ "$(git -C "$ORIGIN" rev-list --parents -n 1 5.3.0)" = "$(rev 5.3.0) $main_before $release_before" ]
}

@test "full release: uses release from origin when the checkout has only main" {
  add_lock main
  copy_branch main release
  bump release "Backport a fix" lodash@4.17.21
  release_before=$(rev release)
  checkout full
  run git -C "$JOB" rev-parse -q --verify refs/heads/release
  [ "$status" -ne 0 ]

  run_script full 5.4.0

  [ "$status" -eq 0 ]
  [ "$(rev 5.4.0^2)" = "$release_before" ]
}

@test "full release: main and release end on the tag, and the commit and the tag are signed" {
  add_lock main
  copy_branch main release
  bump release "Backport a fix" lodash@4.17.21

  release full 5.5.0

  [ "$status" -eq 0 ]
  [ "$(rev main)" = "$(rev 5.5.0)" ]
  [ "$(rev release)" = "$(rev 5.5.0)" ]
  git -C "$ORIGIN" verify-commit "$(rev 5.5.0)"
  git -C "$ORIGIN" verify-tag 5.5.0
}

@test "full release: the release commit skips CI" {
  add_lock main
  copy_branch main release
  bump release "Backport a fix" lodash@4.17.21

  release full 5.6.0

  [ "$status" -eq 0 ]
  [[ "$(git -C "$ORIGIN" log -1 --format=%B 5.6.0)" == *"[skip ci]"* ]]
}

@test "full release: a rerun with release and main on one commit adds no commit" {
  add_lock main
  copy_branch main release
  main_before=$(rev main)

  release full 6.1.0

  [ "$status" -eq 0 ]
  [ "$(rev 6.1.0)" = "$main_before" ]
  [ "$(rev main)" = "$main_before" ]
  [ "$(rev release)" = "$main_before" ]
}

# A hotfix release

@test "hotfix: tags release and merges it into main" {
  add_lock main
  release full 7.1.0
  [ "$status" -eq 0 ]
  commit release app/hotfix.txt hotfix "Fix a bug on release"
  release_before=$(rev release)

  release hotfix 7.1.1

  [ "$status" -eq 0 ]
  [ "$(rev 7.1.1)" = "$release_before" ]
  [ "$(rev release)" = "$release_before" ]
  git -C "$ORIGIN" merge-base --is-ancestor 7.1.1 main
  [ "$(show main app/hotfix.txt)" = hotfix ]
}

@test "hotfix: the merge into main and the tag are signed, and the merge skips CI" {
  add_lock main
  release full 7.2.0
  [ "$status" -eq 0 ]
  commit release app/hotfix.txt hotfix "Fix a bug on release"

  release hotfix 7.2.1

  [ "$status" -eq 0 ]
  git -C "$ORIGIN" verify-commit main
  git -C "$ORIGIN" verify-tag 7.2.1
  [[ "$(git -C "$ORIGIN" log -1 --format=%B main)" == *"[skip ci]"* ]]
}

@test "hotfix: main keeps its side of a conflicting line and gets the other release changes" {
  add_lock main
  release full 7.3.0
  [ "$status" -eq 0 ]
  bump release "Fix a bug on release" lodash@4.17.21 react@18.2.1
  bump main "Update react on main" react@18.3.0

  release hotfix 7.3.1

  [ "$status" -eq 0 ]
  [ "$(lock_at main)" = "lodash@4.17.21 react@18.3.0 axios@1.6.0" ]
}

@test "full release after a hotfix keeps the tree of main" {
  add_lock main
  release full 7.4.0
  [ "$status" -eq 0 ]
  commit release app/hotfix.txt hotfix "Fix a bug on release"
  release hotfix 7.4.1
  [ "$status" -eq 0 ]
  bump main "Update axios on main" axios@1.7.0
  main_before=$(rev main)

  release full 7.5.0

  [ "$status" -eq 0 ]
  [ "$(tree 7.5.0)" = "$(tree "$main_before")" ]
  [ "$(rev release)" = "$(rev 7.5.0)" ]
}

@test "hotfix: a file changed on release and deleted on main stays deleted" {
  commit main app/legacy.txt legacy "Add a legacy file"
  release full 7.6.0
  [ "$status" -eq 0 ]
  commit release app/legacy.txt "legacy, fixed" "Fix the legacy file on release"
  commit release app/hotfix.txt hotfix "Fix a bug on release"
  remove main app/legacy.txt "Remove the legacy file"

  release hotfix 7.6.1

  [ "$status" -eq 0 ]
  run git -C "$ORIGIN" cat-file -e main:app/legacy.txt
  [ "$status" -ne 0 ]
  [ "$(show main app/hotfix.txt)" = hotfix ]
  git -C "$ORIGIN" merge-base --is-ancestor 7.6.1 main
  git -C "$ORIGIN" verify-commit main
  [[ "$(git -C "$ORIGIN" log -1 --format=%B main)" == *"[skip ci]"* ]]
}

@test "hotfix: a file deleted on release and changed on main stays as main has it" {
  commit main app/legacy.txt legacy "Add a legacy file"
  release full 7.6.2
  [ "$status" -eq 0 ]
  remove release app/legacy.txt "Remove the legacy file on release"
  commit main app/legacy.txt "legacy, changed on main" "Change the legacy file on main"

  release hotfix 7.6.3

  [ "$status" -eq 0 ]
  [ "$(show main app/legacy.txt)" = "legacy, changed on main" ]
  git -C "$ORIGIN" merge-base --is-ancestor 7.6.3 main
}

@test "hotfix: resolving a file with a glob character in its name keeps it deleted, and only it" {
  commit main 'docs/*.md' pattern "Add a file with a glob character in its name"
  commit main docs/a.md a "Add a Markdown file"
  release full 7.6.4
  [ "$status" -eq 0 ]
  commit release 'docs/*.md' "pattern, fixed" "Fix the pattern file on release"
  commit release docs/a.md "a, fixed" "Fix the Markdown file on release"
  remove main 'docs/*.md' "Remove the pattern file on main"

  release hotfix 7.6.5

  [ "$status" -eq 0 ]
  run git -C "$ORIGIN" cat-file -e 'main:docs/*.md'
  [ "$status" -ne 0 ]
  [ "$(show main docs/a.md)" = "a, fixed" ]
}

@test "hotfix: resolving a file with a glob character in its name keeps the file of main, and only it" {
  commit main 'docs/*.md' pattern "Add a file with a glob character in its name"
  commit main docs/a.md a "Add a Markdown file"
  release full 7.6.6
  [ "$status" -eq 0 ]
  remove release 'docs/*.md' "Remove the pattern file on release"
  commit release docs/a.md "a, fixed" "Fix the Markdown file on release"
  commit main 'docs/*.md' "pattern, changed on main" "Change the pattern file on main"

  release hotfix 7.6.7

  [ "$status" -eq 0 ]
  [ "$(show main 'docs/*.md')" = "pattern, changed on main" ]
  [ "$(show main docs/a.md)" = "a, fixed" ]
}

@test "hotfix: a rename/rename conflict stops the job, and nothing is pushed" {
  commit main app/legacy.txt legacy "Add a legacy file"
  release full 7.8.0
  [ "$status" -eq 0 ]
  commit release app/release-name.txt legacy "Copy the legacy file on release"
  remove release app/legacy.txt "Remove the legacy file on release"
  commit main app/main-name.txt legacy "Copy the legacy file on main"
  remove main app/legacy.txt "Remove the legacy file on main"
  before=$(origin_refs)

  release hotfix 7.8.1

  [ "$status" -ne 0 ]
  [ "$(origin_refs)" = "$before" ]
}

@test "hotfix: a file/directory conflict stops the job, and nothing is pushed" {
  add_lock main
  release full 7.8.2
  [ "$status" -eq 0 ]
  commit release app/config/settings settings "Add a config folder on release"
  commit main app/config main "Add a config file on main"
  before=$(origin_refs)

  release hotfix 7.8.3

  [ "$status" -ne 0 ]
  [ "$(origin_refs)" = "$before" ]
}

@test "hotfix: a merge into main that fails pushes nothing, not even the tag" {
  add_lock main
  release full 7.7.0
  [ "$status" -eq 0 ]
  commit release app/hotfix.txt hotfix "Fix a bug on release"
  before=$(origin_refs)
  checkout hotfix
  printf '#!/bin/sh\necho "merge refused by hook"\nexit 1\n' > "$JOB/.git/hooks/pre-merge-commit"
  chmod +x "$JOB/.git/hooks/pre-merge-commit"

  run_script hotfix 7.7.1

  [ "$status" -ne 0 ]
  [[ "$output" == *"merge refused by hook"* ]]
  [ "$(origin_refs)" = "$before" ]
}

# Failures

@test "stops before pushing when the release commit cannot be signed" {
  add_lock main
  copy_branch main release
  bump release "Backport a fix" lodash@4.17.21
  before=$(origin_refs)

  release full 8.1.0 "${NO_SIGNING[@]}"

  [ "$status" -ne 0 ]
  [[ "$output" == *"failed to sign"* ]]
  [ "$(origin_refs)" = "$before" ]
}

@test "stops before pushing when the tag cannot be signed" {
  add_lock main
  copy_branch main release
  before=$(origin_refs)

  release full 8.2.0 "${NO_SIGNING[@]}"

  [ "$status" -ne 0 ]
  [[ "$output" == *"failed to sign"* ]]
  [ "$(origin_refs)" = "$before" ]
}

@test "stops before pushing when the version is missing" {
  add_lock main
  copy_branch main release
  bump release "Backport a fix" lodash@4.17.21
  before=$(origin_refs)

  release full ""

  [ "$status" -ne 0 ]
  [[ "$output" == *VERSION* ]]
  [ "$(origin_refs)" = "$before" ]
}

@test "stops before pushing when the release type is unknown" {
  add_lock main
  copy_branch main release
  before=$(origin_refs)

  release major 8.3.0

  [ "$status" -ne 0 ]
  [[ "$output" == *major* ]]
  [ "$(origin_refs)" = "$before" ]
}

@test "stops before pushing when the tag already exists, as on a rerun after deleting only the GitHub release" {
  add_lock main
  release full 8.4.0
  [ "$status" -eq 0 ]
  before=$(origin_refs)

  release full 8.4.0

  [ "$status" -ne 0 ]
  [[ "$output" == *"already exists"* ]]
  [ "$(origin_refs)" = "$before" ]
}

@test "fails when main moved in origin after the checkout" {
  add_lock main
  copy_branch main release
  bump release "Backport a fix" lodash@4.17.21
  checkout full
  commit main app/late.txt late "Merge a pull request during the release"

  run_script full 8.5.0

  [ "$status" -ne 0 ]
  [[ "$output" == *rejected* ]]
}

@test "a push that main rejects leaves release and the tags in origin unchanged" {
  add_lock main
  copy_branch main release
  bump release "Backport a fix" lodash@4.17.21
  checkout full
  commit main app/late.txt late "Merge a pull request during the release"
  release_before=$(rev release)

  run_script full 8.6.0

  [ "$status" -ne 0 ]
  [[ "$output" == *rejected* ]]
  [ "$(rev release)" = "$release_before" ]
  run git -C "$ORIGIN" rev-parse -q --verify refs/tags/8.6.0
  [ "$status" -ne 0 ]
}

@test "hotfix: a push that main rejects leaves the tags in origin unchanged" {
  add_lock main
  release full 8.7.0
  [ "$status" -eq 0 ]
  commit release app/hotfix.txt hotfix "Fix a bug on release"
  checkout hotfix
  commit main app/late.txt late "Merge a pull request during the release"

  run_script hotfix 8.7.1

  [ "$status" -ne 0 ]
  [[ "$output" == *rejected* ]]
  run git -C "$ORIGIN" rev-parse -q --verify refs/tags/8.7.1
  [ "$status" -ne 0 ]
}
