#!/usr/bin/env bash
# Merge a directory of generated files into the `data` branch.
#
# The data branch always holds exactly one commit (force-pushed), so the
# monthly catalog and weekly forecasts don't pile up in git history.
#
# Usage: scripts/publish_data.sh <dir> "<commit message>"
set -euo pipefail

SRC="$(cd "$1" && pwd)"
MSG="$2"
WORK="$(mktemp -d)"
cleanup() { git worktree remove --force "$WORK" >/dev/null 2>&1 || true; }
trap cleanup EXIT

if git ls-remote --exit-code --heads origin data >/dev/null 2>&1; then
  git fetch --depth 1 origin "+refs/heads/data:refs/remotes/origin/data"
  git worktree add --detach "$WORK" origin/data
else
  git worktree add --detach "$WORK"
  (cd "$WORK" && git rm -rq --ignore-unmatch . && git clean -fdxq)
fi

cp -a "$SRC"/. "$WORK"/
cd "$WORK"
git add -A
TREE="$(git write-tree)"
COMMIT="$(git -c user.name="trainhead-bot" -c user.email="trainhead-bot@users.noreply.github.com" \
  commit-tree "$TREE" -m "$MSG")"
git push -f origin "$COMMIT:refs/heads/data"
echo "published to data branch: $MSG"
