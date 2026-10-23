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
    git merge origin/release -X ours --no-ff \
      -m "Merge Release $VERSION into main" -m "[skip ci]"
    git push --atomic origin main "$VERSION"
    ;;
  *)
    echo "::error::Unknown release type: $RELEASE_TYPE" >&2
    exit 1
    ;;
esac
