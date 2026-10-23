#!/usr/bin/env bats

# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: GPL-3.0-or-later

setup() {
  SCRIPT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/sync-container-files.sh"

  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
  export GIT_AUTHOR_NAME=bot GIT_AUTHOR_EMAIL=bot@example.com
  export GIT_COMMITTER_NAME=bot GIT_COMMITTER_EMAIL=bot@example.com

  export SOURCE_DIR="$BATS_TEST_TMPDIR/source"
  export TARGET_DIR="$BATS_TEST_TMPDIR/target"
  export REPOSITORY=trento-project/web
  export CHANGES_FILE=trento-web-image.changes
  export AUTHOR_EMAIL=trento-developers@suse.com
  export GITHUB_API_URL="file://$BATS_TEST_TMPDIR/api"
  export GITHUB_OUTPUT="$BATS_TEST_TMPDIR/github_output"
  : > "$GITHUB_OUTPUT"

  git init -q -b main "$SOURCE_DIR"
  git init -q -b main "$TARGET_DIR"
  printf 'FROM old\n' > "$TARGET_DIR/Dockerfile"
  printf '<services/>\n' > "$TARGET_DIR/_service"
  printf '%s\n' \
    "-------------------------------------------------------------------" \
    "Wed May 20 08:13:10 UTC 2026 - trento-developers@suse.com" \
    "" \
    "- Update Dockerfile to create image based on trento-web rpm" \
    "" > "$TARGET_DIR/$CHANGES_FILE"
  git -C "$TARGET_DIR" add -A
  git -C "$TARGET_DIR" commit -q -m "initial"
}

# commit_source FILE CONTENT MESSAGE: write FILE (relative to the source
# repository) and commit it. Prints the new commit SHA.
commit_source() {
  mkdir -p "$(dirname "$SOURCE_DIR/$1")"
  printf '%s\n' "$2" > "$SOURCE_DIR/$1"
  git -C "$SOURCE_DIR" add -A
  git -C "$SOURCE_DIR" commit -q -m "$3"
  git -C "$SOURCE_DIR" rev-parse HEAD
}

# pulls_for COMMIT JSON: answer the GitHub "pull requests associated with a
# commit" endpoint for COMMIT with JSON.
pulls_for() {
  mkdir -p "$BATS_TEST_TMPDIR/api/repos/$REPOSITORY/commits/$1"
  printf '%s\n' "$2" > "$BATS_TEST_TMPDIR/api/repos/$REPOSITORY/commits/$1/pulls"
}

# merged_pr NUMBER: one merged pull request, as the GitHub API returns it.
merged_pr() {
  printf '{"number": %s, "state": "closed", "title": "PR %s", "html_url": "https://github.com/%s/pull/%s", "merged_at": "2026-10-13T09:00:00Z", "base": {"ref": "main"}, "head": {"ref": "branch-%s"}}' \
    "$1" "$1" "$REPOSITORY" "$1" "$1"
}

# entry_lines: the lines of the newest .changes entry, without the date line.
entry_lines() {
  awk 'NR == 1 { next } /^-{60,}$/ { exit } NR == 2 { next } { print }' "$TARGET_DIR/$CHANGES_FILE"
}

@test "copies the container folder at SOURCE_REF into the target and commits" {
  commit_source packaging/suse/container/Dockerfile "FROM new" "Add Dockerfile" > /dev/null
  SOURCE_REF=$(commit_source packaging/suse/container/README.md "readme new" "Add README")
  export SOURCE_REF

  run "$SCRIPT"

  [ "$status" -eq 0 ]
  [ "$(cat "$TARGET_DIR/Dockerfile")" = "FROM new" ]
  [ "$(cat "$TARGET_DIR/README.md")" = "readme new" ]
  [ "$(git -C "$TARGET_DIR" rev-list --count HEAD)" -eq 2 ]
  [ -z "$(git -C "$TARGET_DIR" status --porcelain)" ]
  grep -qxF "changed=true" "$GITHUB_OUTPUT"
}

@test "syncs the folder as it is at SOURCE_REF, not at the checked-out HEAD" {
  SOURCE_REF=$(commit_source packaging/suse/container/Dockerfile "FROM tagged" "Release")
  export SOURCE_REF
  commit_source packaging/suse/container/Dockerfile "FROM later" "Later change" > /dev/null

  run "$SCRIPT"

  [ "$status" -eq 0 ]
  [ "$(cat "$TARGET_DIR/Dockerfile")" = "FROM tagged" ]
}

@test "makes no commit and keeps .changes when the files are already in sync" {
  SOURCE_REF=$(commit_source packaging/suse/container/Dockerfile "FROM old" "Same content")
  export SOURCE_REF
  changes_before=$(cat "$TARGET_DIR/$CHANGES_FILE")

  run "$SCRIPT"

  [ "$status" -eq 0 ]
  [ "$(git -C "$TARGET_DIR" rev-list --count HEAD)" -eq 1 ]
  [ "$(cat "$TARGET_DIR/$CHANGES_FILE")" = "$changes_before" ]
  grep -qxF "changed=false" "$GITHUB_OUTPUT"
}

@test "first sync: the .changes entry links the PR of the last commit that changed the folder" {
  folder_commit=$(commit_source packaging/suse/container/Dockerfile "FROM new" "Change Dockerfile")
  pulls_for "$folder_commit" "[$(merged_pr 4288)]"
  SOURCE_REF=$(commit_source lib/app.ex "code" "Unrelated change")
  pulls_for "$SOURCE_REF" "[$(merged_pr 4999)]"
  export SOURCE_REF

  run "$SCRIPT"

  [ "$status" -eq 0 ]
  [ "$(sed -n 1p "$TARGET_DIR/$CHANGES_FILE")" = "-------------------------------------------------------------------" ]
  sed -n 2p "$TARGET_DIR/$CHANGES_FILE" \
    | grep -qE '^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Z][a-z]{2} [0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} UTC 20[0-9]{2} - trento-developers@suse.com$'
  [ "$(entry_lines)" = "
- Sync container files with trento-project/web@${SOURCE_REF:0:10}
  * https://github.com/trento-project/web/pull/4288" ]
  [ "$(sed -n 7,8p "$TARGET_DIR/$CHANGES_FILE")" = "-------------------------------------------------------------------
Wed May 20 08:13:10 UTC 2026 - trento-developers@suse.com" ]
}

# record_sync COMMIT: add a .changes entry to the target, in the format of
# the manual syncs, that records COMMIT as synced.
record_sync() {
  {
    printf '%s\n' \
      "-------------------------------------------------------------------" \
      "Wed Oct 21 06:38:12 UTC 2026 - trento-developers@suse.com" \
      "" \
      "- Sync Dockerfile and README.md with $REPOSITORY@${1:0:10}:" \
      "  * Update the license to GPL-3.0-or-later" \
      ""
    cat "$TARGET_DIR/$CHANGES_FILE"
  } > "$TARGET_DIR/changes.tmp"
  mv "$TARGET_DIR/changes.tmp" "$TARGET_DIR/$CHANGES_FILE"
  git -C "$TARGET_DIR" commit -q -am "Manual sync"
}

@test "links the PRs of the commits that changed the folder since the last synced commit" {
  before=$(commit_source packaging/suse/container/Dockerfile "FROM 1" "Before the last sync")
  pulls_for "$before" "[$(merged_pr 4800)]"
  synced=$(commit_source packaging/suse/container/Dockerfile "FROM 2" "Last synced")
  pulls_for "$synced" "[$(merged_pr 4850)]"
  record_sync "$synced"
  first=$(commit_source packaging/suse/container/Dockerfile "FROM 3" "First change")
  pulls_for "$first" "[$(merged_pr 4915)]"
  unrelated=$(commit_source lib/app.ex "code" "Unrelated change")
  pulls_for "$unrelated" "[$(merged_pr 4950)]"
  SOURCE_REF=$(commit_source packaging/suse/container/README.md "readme" "Second change")
  pulls_for "$SOURCE_REF" "[$(merged_pr 4900)]"
  export SOURCE_REF

  run "$SCRIPT"

  [ "$status" -eq 0 ]
  [ "$(entry_lines)" = "
- Sync container files with trento-project/web@${SOURCE_REF:0:10}
  * https://github.com/trento-project/web/pull/4900
  * https://github.com/trento-project/web/pull/4915" ]
}

@test "deletes the files removed from the folder since the last synced commit" {
  commit_source packaging/suse/container/_constraints "constraints" "Add constraints" > /dev/null
  synced=$(commit_source packaging/suse/container/Dockerfile "FROM old" "Last synced")
  printf 'constraints\n' > "$TARGET_DIR/_constraints"
  git -C "$TARGET_DIR" add -A
  git -C "$TARGET_DIR" commit -q -m "Synced constraints"
  record_sync "$synced"
  git -C "$SOURCE_DIR" rm -q packaging/suse/container/_constraints
  git -C "$SOURCE_DIR" commit -q -m "Drop constraints"
  SOURCE_REF=$(git -C "$SOURCE_DIR" rev-parse HEAD)
  export SOURCE_REF

  run "$SCRIPT"

  [ "$status" -eq 0 ]
  [ ! -e "$TARGET_DIR/_constraints" ]
  [ -e "$TARGET_DIR/_service" ]
  [ -z "$(git -C "$TARGET_DIR" status --porcelain)" ]
}

@test "lists each merged PR once and skips PRs that were never merged" {
  synced=$(commit_source packaging/suse/container/Dockerfile "FROM old" "Last synced")
  record_sync "$synced"
  first=$(commit_source packaging/suse/container/Dockerfile "FROM 1" "First commit of the PR")
  pulls_for "$first" "[$(merged_pr 4915)]"
  SOURCE_REF=$(commit_source packaging/suse/container/Dockerfile "FROM 2" "Second commit of the PR")
  pulls_for "$SOURCE_REF" "[$(merged_pr 4915), {\"number\": 4999, \"state\": \"open\", \"title\": \"PR 4999\", \"html_url\": \"https://github.com/$REPOSITORY/pull/4999\", \"merged_at\": null, \"base\": {\"ref\": \"main\"}, \"head\": {\"ref\": \"branch-4999\"}}]"
  export SOURCE_REF

  run "$SCRIPT"

  [ "$status" -eq 0 ]
  [ "$(entry_lines)" = "
- Sync container files with trento-project/web@${SOURCE_REF:0:10}
  * https://github.com/trento-project/web/pull/4915" ]
}

@test "still syncs, without PR links, when the PR lookup fails" {
  SOURCE_REF=$(commit_source packaging/suse/container/Dockerfile "FROM new" "Change")
  export SOURCE_REF GITHUB_API_URL="file://$BATS_TEST_TMPDIR/no-api"

  run "$SCRIPT"

  [ "$status" -eq 0 ]
  [[ "$output" == *"::warning::Could not look up the pull requests of trento-project/web@$SOURCE_REF."* ]]
  [ "$(entry_lines)" = "
- Sync container files with trento-project/web@${SOURCE_REF:0:10}" ]
  [ "$(git -C "$TARGET_DIR" rev-list --count HEAD)" -eq 2 ]
}

@test "the commit message names the synced commit" {
  SOURCE_REF=$(commit_source packaging/suse/container/Dockerfile "FROM new" "Change")
  export SOURCE_REF

  run "$SCRIPT"

  [ "$status" -eq 0 ]
  [ "$(git -C "$TARGET_DIR" log -1 --format=%s)" = "Sync container files with trento-project/web@${SOURCE_REF:0:10}" ]
}

@test "refuses a symlink in the container folder and leaves the target untouched" {
  printf 'secret\n' > "$BATS_TEST_TMPDIR/secret"
  mkdir -p "$SOURCE_DIR/packaging/suse/container"
  ln -s "$BATS_TEST_TMPDIR/secret" "$SOURCE_DIR/packaging/suse/container/Dockerfile"
  git -C "$SOURCE_DIR" add -A
  git -C "$SOURCE_DIR" commit -q -m "Add a symlink"
  SOURCE_REF=$(git -C "$SOURCE_DIR" rev-parse HEAD)
  export SOURCE_REF

  run "$SCRIPT"

  [ "$status" -ne 0 ]
  [ "$(git -C "$TARGET_DIR" rev-list --count HEAD)" -eq 1 ]
  [ -z "$(git -C "$TARGET_DIR" status --porcelain)" ]
  [ ! -L "$TARGET_DIR/Dockerfile" ]
}

@test "refuses a container folder that would overwrite the .changes file" {
  SOURCE_REF=$(commit_source "packaging/suse/container/$CHANGES_FILE" "forged entry" "Add a changes file")
  export SOURCE_REF

  run "$SCRIPT"

  [ "$status" -ne 0 ]
  [ "$(git -C "$TARGET_DIR" rev-list --count HEAD)" -eq 1 ]
  [ -z "$(git -C "$TARGET_DIR" status --porcelain)" ]
}

@test "refuses a .changes file in the target that is not a regular file" {
  printf 'secret\n' > "$BATS_TEST_TMPDIR/secret"
  rm "$TARGET_DIR/$CHANGES_FILE"
  ln -s "$BATS_TEST_TMPDIR/secret" "$TARGET_DIR/$CHANGES_FILE"
  git -C "$TARGET_DIR" add -A
  git -C "$TARGET_DIR" commit -q -m "Symlinked changes"
  SOURCE_REF=$(commit_source packaging/suse/container/Dockerfile "FROM new" "Change")
  export SOURCE_REF

  run "$SCRIPT"

  [ "$status" -ne 0 ]
  [ "$(git -C "$TARGET_DIR" rev-list --count HEAD)" -eq 2 ]
  [ "$(cat "$BATS_TEST_TMPDIR/secret")" = "secret" ]
}

@test "keeps files that exist only in the target" {
  SOURCE_REF=$(commit_source packaging/suse/container/Dockerfile "FROM new" "Change")
  export SOURCE_REF

  run "$SCRIPT"

  [ "$status" -eq 0 ]
  [ "$(cat "$TARGET_DIR/_service")" = "<services/>" ]
}
