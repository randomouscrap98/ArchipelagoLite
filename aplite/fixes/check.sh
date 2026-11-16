#!/bin/sh
# Fails when aplite/fixes/fixes.py changed but its LOG didn't get a new line, compared with origin/main (what users
# download; as of your last fetch) and with the last release tag before HEAD (what that release bundled).
# Run it before pushing a fixes change to main; release.sh and build_dist.sh run it too.
# --pick (on another branch): also cherry-picks the last commit, which must change only fixes.py, onto local main,
# checks there, and switches back. It never pushes.
set -eu
PATH="$PATH:$HOME/.local/bin"
REPO=$(cd "$(dirname "$0")/../.." && pwd)
FIXES=aplite/fixes/fixes.py

check() {
    refs="origin/main"
    if tag=$(git -C "$REPO" describe --tags --abbrev=0 --match 'v*-lite.*' HEAD~1 2>/dev/null); then
        refs="$refs $tag"
    fi
    # shellcheck disable=SC2086
    PYTHONPATH="$REPO" uv run --quiet --no-project --python 3.13 python -m aplite.client_fixes --check $refs
}

case "${1:-}" in
    "") check; exit ;;
    --pick) ;;
    *) echo "usage: $0 [--pick]" >&2; exit 2 ;;
esac

cd "$REPO"
branch=$(git symbolic-ref --short HEAD)
[ "$branch" != main ] || { echo "--pick: already on main" >&2; exit 1; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "--pick: commit or stash your changes first" >&2; exit 1; }
changed=$(git diff-tree --no-commit-id --name-only -r HEAD)
[ "$changed" = "$FIXES" ] || { printf -- "--pick: last commit must change only %s, it changes:\n%s\n" "$FIXES" "$changed" >&2; exit 1; }
commit=$(git rev-parse HEAD)
case "$(git cherry main "$commit" "$commit~1")" in
    -*) echo "--pick: main already has this change" >&2; exit 1 ;;
esac
check
git switch --quiet main
if git cherry-pick "$commit" && check; then
    git switch --quiet "$branch"
    echo "Picked $(git log --oneline -1 "$commit") onto main. Push main when ready."
else
    git cherry-pick --abort 2>/dev/null || true
    git switch --quiet "$branch"
    echo "--pick failed; back on $branch. main may have the commit if only the check on main failed (git log main)." >&2
    exit 1
fi
