#!/bin/sh
# Fails when aplite/fixes/fixes.py changed but its LOG didn't get a new line, compared with origin/main (what users
# download; as of your last fetch) and with the last release tag before HEAD (what that release bundled).
# Run it before pushing a fixes change to main; release.sh and build_dist.sh run it too. It never commits.
set -eu
PATH="$PATH:$HOME/.local/bin"
REPO=$(cd "$(dirname "$0")/../.." && pwd)
refs="origin/main"
if tag=$(git -C "$REPO" describe --tags --abbrev=0 --match 'v*-lite.*' HEAD~1 2>/dev/null); then
    refs="$refs $tag"
fi
# shellcheck disable=SC2086
PYTHONPATH="$REPO" uv run --quiet --no-project --python 3.13 python -m aplite.client_fixes --check $refs
