#!/usr/bin/env bash

# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: GPL-3.0-or-later

# Tag a release, merge the main and release branches, and push.
# Run it in a checkout of main for a full release, or of release for a hotfix
# release, with commit and tag signing enabled.

set -euo pipefail

: "${RELEASE_TYPE:?}" "${VERSION:?}"

case "$RELEASE_TYPE" in
  full)
    git branch -f release origin/release 2> /dev/null || git branch -f release main

    # `git merge -X theirs` favors main only for conflicting changes, so
    # changes that exist only on release leaked into the release. The ours
    # strategy takes the tree of main (the current branch) as is.
    git merge release -s ours -m "Release $VERSION" -m "[skip ci]"
    git branch -f release main
    git tag "$VERSION" -m "Release $VERSION"
    # All refs or none, so that a rejected main leaves no tag behind.
    git push --atomic origin main release "$VERSION"
    ;;
  hotfix)
    git tag "$VERSION" -m "Release $VERSION"
    git switch main
    message=(-m "Merge Release $VERSION into main" -m "[skip ci]")
    if ! git merge origin/release -X ours --no-ff "${message[@]}"; then
      # -X ours resolves conflicting lines for main, but not a file that one
      # side deleted and the other side changed. Resolve these files for main
      # too: keep the file of main, or keep it deleted. Stop on any other
      # failure of the merge.
      settled=0
      while IFS= read -r -d '' path; do
        if git cat-file -e "HEAD:$path" 2> /dev/null; then
          git checkout HEAD -- "$path"
        else
          git rm -q -- "$path"
        fi
        settled=$((settled + 1))
      done < <(git diff -z --name-only --diff-filter=U)
      [ "$settled" -gt 0 ] || exit 1
      git commit "${message[@]}"
    fi
    git push --atomic origin main "$VERSION"
    ;;
  *)
    echo "::error::Unknown release type: $RELEASE_TYPE" >&2
    exit 1
    ;;
esac
