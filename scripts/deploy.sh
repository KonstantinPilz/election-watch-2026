#!/usr/bin/env bash
# Fetch fresh prices and publish site/ to the gh-pages branch (one orphan commit,
# force-pushed, so the branch never piles up history). Run by cron every 6 min:
# branch-built GitHub Pages allows about 10 builds per hour.
# Stops by itself after 2026-11-10.
set -euo pipefail
[ "$(date -u +%Y%m%d)" -gt 20261110 ] && exit 0
cd "$(dirname "$0")/.."
exec 9>/tmp/election-watch-deploy.lock
flock -n 9 || exit 0
python3 fetch.py site
touch site/.nojekyll
export GIT_INDEX_FILE
GIT_INDEX_FILE=$(mktemp -u /tmp/election-watch-index.XXXXXX)
trap 'rm -f "$GIT_INDEX_FILE"' EXIT
git --work-tree=site add -f index.html data.json .nojekyll
tree=$(git write-tree)
export GIT_AUTHOR_NAME=election-watch GIT_AUTHOR_EMAIL=election-watch@users.noreply.github.com
export GIT_COMMITTER_NAME=$GIT_AUTHOR_NAME GIT_COMMITTER_EMAIL=$GIT_AUTHOR_EMAIL
commit=$(git commit-tree "$tree" -m "data $(date -u +%FT%TZ)")
git push -q -f origin "$commit:refs/heads/gh-pages"
echo "$(date -u +%FT%TZ) deployed $commit"
