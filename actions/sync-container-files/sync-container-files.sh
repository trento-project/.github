#!/usr/bin/env bash

# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: GPL-3.0-or-later

# Sync the container folder of a GitHub repository checkout into the Git
# repository of the matching OBS image package, and commit the result.

set -euo pipefail

: "${SOURCE_DIR:?}" "${SOURCE_REF:?}" "${TARGET_DIR:?}" "${REPOSITORY:?}" \
  "${CHANGES_FILE:?}" "${AUTHOR_EMAIL:?}"
CONTAINER_DIR=${CONTAINER_DIR:-packaging/suse/container}
GITHUB_API_URL=${GITHUB_API_URL:-https://api.github.com}

# merged_prs COMMIT: print "NUMBER URL" for each merged pull request that
# contains COMMIT.
merged_prs() {
  python3 - "$GITHUB_API_URL/repos/$REPOSITORY/commits/$1/pulls" << 'EOF'
import json, os, sys, urllib.request

request = urllib.request.Request(sys.argv[1], headers={"Accept": "application/vnd.github+json"})
token = os.environ.get("GITHUB_TOKEN")
if token and sys.argv[1].startswith("https://"):
    request.add_header("Authorization", f"Bearer {token}")
with urllib.request.urlopen(request, timeout=30) as response:
    for pr in json.load(response):
        if pr.get("merged_at"):
            print(pr["number"], pr["html_url"])
EOF
}

ref=$(git -C "$SOURCE_DIR" rev-parse --verify "${SOURCE_REF}^{commit}")
depth=$(tr -cd / <<< "${CONTAINER_DIR%/}/" | wc -c)
changes="$TARGET_DIR/$CHANGES_FILE"

# fail MESSAGE: report MESSAGE as a GitHub error and stop.
fail() {
  echo "::error::$1" >&2
  exit 1
}

# A symlink could make the sync read and publish files of the runner, such
# as the SSH key or the process environment. Read the full listing first,
# so that the check cannot depend on the exit status of a pipe.
tree=$(git -C "$SOURCE_DIR" ls-tree -r "$ref" -- "$CONTAINER_DIR")
if awk '$1 == "120000" || $1 == "160000" { found = 1 } END { exit !found }' <<< "$tree"; then
  fail "$CONTAINER_DIR at $REPOSITORY@$ref contains a symlink or a submodule. Refusing to sync."
fi
if git -C "$SOURCE_DIR" cat-file -e "$ref:${CONTAINER_DIR%/}/$CHANGES_FILE" 2> /dev/null; then
  fail "$CONTAINER_DIR contains $CHANGES_FILE, which only the sync writes. Refusing to sync."
fi
if [ ! -f "$changes" ] || [ -L "$changes" ]; then
  fail "$CHANGES_FILE in the target is missing or is not a regular file. Refusing to sync."
fi

# The newest .changes entry that names a synced commit of this repository.
synced=$(grep -m1 -oE "Sync .+ with $REPOSITORY@[0-9a-f]{7,40}" "$changes" | sed 's/.*@//' || true)
if [ -n "$synced" ]; then
  synced=$(git -C "$SOURCE_DIR" rev-parse --verify --quiet "${synced}^{commit}" || true)
fi

if [ -n "$synced" ]; then
  git -C "$SOURCE_DIR" diff -z --name-only --no-renames --diff-filter=D "$synced" "$ref" -- "$CONTAINER_DIR" \
    | while IFS= read -r -d '' path; do
      rm -f "$TARGET_DIR/${path#"${CONTAINER_DIR%/}/"}"
    done
fi

git -C "$SOURCE_DIR" archive --format=tar "$ref" "$CONTAINER_DIR" \
  | tar -x -C "$TARGET_DIR" --strip-components="$depth"

git -C "$TARGET_DIR" add -A
if git -C "$TARGET_DIR" diff --cached --quiet; then
  echo "Container files are already in sync."
  echo "changed=false" >> "$GITHUB_OUTPUT"
  exit 0
fi

if [ -n "$synced" ]; then
  commits=$(git -C "$SOURCE_DIR" log --format=%H "$synced..$ref" -- "$CONTAINER_DIR")
else
  commits=$(git -C "$SOURCE_DIR" log -1 --format=%H "$ref" -- "$CONTAINER_DIR")
fi
summary="Sync container files with $REPOSITORY@${ref:0:10}"
prs=$(for commit in $commits; do
  merged_prs "$commit" 2> /dev/null \
    || echo "::warning::Could not look up the pull requests of $REPOSITORY@$commit." >&2
done | sort -n -u)

{
  echo "-------------------------------------------------------------------"
  echo "$(LC_ALL=C date -u '+%a %b %d %H:%M:%S UTC %Y') - $AUTHOR_EMAIL"
  echo
  echo "- $summary"
  while read -r _ url; do
    if [ -n "$url" ]; then
      echo "  * $url"
    fi
  done <<< "$prs"
  echo
  cat "$changes"
} > "$changes.new"
mv "$changes.new" "$changes"

git -C "$TARGET_DIR" add -A
git -C "$TARGET_DIR" commit -q -m "$summary"
echo "changed=true" >> "$GITHUB_OUTPUT"
