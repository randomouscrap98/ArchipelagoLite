#!/bin/sh
# Tags the next release (v<Archipelago version>-lite.N, N = highest existing for that version + 1), pushes the tag,
# then rebuilds dist/ from scratch with build_dist.sh. Needs a clean working tree.
# Env: REMOTE (default origin)
set -eu
PATH="$PATH:$HOME/.local/bin"

REPO=$(cd "$(dirname "$0")/.." && pwd)
REMOTE=${REMOTE:-origin}
cd "$REPO"

[ -z "$(git status --porcelain)" ] || { echo "release: uncommitted changes, commit them first" >&2; exit 1; }
if existing=$(git describe --tags --exact-match --match 'v*-lite.*' 2>/dev/null); then
    echo "release: HEAD is already tagged $existing" >&2
    exit 1
fi

git fetch --quiet --tags "$REMOTE"
upstream=$(PYTHONPATH="$REPO" uv run --quiet --no-project --python 3.13 python -m aplite.version --upstream)
last=$(git tag --list "v$upstream-lite.*" | sed -n "s/^v$upstream-lite\.\([0-9][0-9]*\)$/\1/p" | sort -n | tail -1)
tag="v$upstream-lite.$(( ${last:-0} + 1 ))"

printf 'Tag %s at %s and push it to %s? [y/N] ' "$tag" "$(git log -1 --format='%h %s')" "$REMOTE"
read -r answer
case $answer in [yY]*) ;; *) echo "release: cancelled"; exit 1 ;; esac

git tag "$tag"
git push "$REMOTE" "$tag"

rm -rf "$REPO/dist"
sh "$REPO/aplite/build_dist.sh"
