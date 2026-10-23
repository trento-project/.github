#!/usr/bin/env bash

# SPDX-FileCopyrightText: SUSE LLC
# SPDX-License-Identifier: GPL-3.0-or-later

# Tag a release, bring the main and release branches together, and push.
# Run it in a checkout of main for a full release, or of release for a hotfix
# release, with signing on for commits and tags.

set -euo pipefail

: "${RELEASE_TYPE:?}" "${VERSION:?}"

case "$RELEASE_TYPE" in
  full)
    git switch release 2> /dev/null || git switch -c release main

    # `git merge -X theirs` favors main only in conflicting hunks, so changes
    # that exist only on release leaked into the release. Build the merge
    # commit from the tree of main instead.
    # Make no empty commit when release and main are the same commit, as on
    # the first release of a repository or on a rerun. In every other case,
    # even with release ahead of main, the merge keeps the tree of main.
    if [ "$(git rev-parse release)" != "$(git rev-parse main)" ]; then
      commit=$(git commit-tree -S 'main^{tree}' -p release -p main \
                 -m "Release $VERSION" -m "[skip ci]")
      git merge --ff-only "$commit"
    fi

    git switch main
    git merge release --ff-only
    git tag "$VERSION" -m "Release $VERSION"
    git push origin main release "$VERSION"
    ;;
  hotfix)
    git tag "$VERSION" -m "Release $VERSION"
    git switch main
    git merge origin/release -X ours --no-ff \
      -m "Merge Release $VERSION into main" -m "[skip ci]"
    git push origin main "$VERSION"
    ;;
  *)
    echo "::error::Unknown release type: $RELEASE_TYPE" >&2
    exit 1
    ;;
esac
