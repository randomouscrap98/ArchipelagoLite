#!/bin/sh
# ArchipelagoLite launcher: runs `python -m aplite` in a uv-managed, kivy-free Python 3.13 venv.
# Usage: ./aplite.sh [Patch|Game|Component|url] [component args]
#        ./aplite.sh --sync    force a dependency reinstall
# APLITE_VENV overrides the venv location (e.g. one per machine when the repo is shared)
set -eu

SELF=$(readlink -f "$0" 2>/dev/null || echo "$0")
REPO=$(cd "$(dirname "$SELF")" && pwd)
VENV="${APLITE_VENV:-$REPO/.venv-aplite}"
PY="$VENV/bin/python"
STAMP="$VENV/.aplite-stamp"

if ! command -v uv >/dev/null 2>&1; then
    echo "aplite needs uv (https://docs.astral.sh/uv/). Install it with:" >&2
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    echo "or your package manager (e.g. pacman -S uv, brew install uv)." >&2
    exit 1
fi

# python symlink breaks if uv's managed python is removed; recreate the venv then
if ! "$PY" -V >/dev/null 2>&1; then
    rm -rf "$VENV"
    uv venv --quiet --python 3.13 "$VENV"
fi

# same files ModuleUpdate checks, plus aplite's own requirements and this script
stamp=$(cat "$REPO/requirements.txt" "$REPO"/worlds/*/requirements.txt "$REPO/aplite/requirements.txt" \
    "$REPO/aplite/requirements.py" "$SELF" | cksum)

if [ "${1:-}" = "--sync" ] || [ "$(cat "$STAMP" 2>/dev/null)" != "$stamp" ]; then
    echo "aplite: installing dependencies..." >&2
    "$PY" "$REPO/aplite/requirements.py" "$REPO/requirements.txt" > "$VENV/requirements-lite.txt"
    uv pip install --quiet --python "$PY" -r "$VENV/requirements-lite.txt" -r "$REPO/aplite/requirements.txt"
    # built-in world requirements (custom apworlds may rely on these); if a file fails as a whole,
    # install its packages one by one so one unbuildable package doesn't take the rest down
    for r in "$REPO"/worlds/*/requirements.txt; do
        uv pip install --quiet --python "$PY" -r "$r" 2>/dev/null || {
            "$PY" "$REPO/aplite/requirements.py" --each "$r" | while read -r req; do
                uv pip install --quiet --python "$PY" "$req" 2>/dev/null || echo "aplite: WARNING: could not install $req ($r)" >&2
            done
        }
    done
    echo "$stamp" > "$STAMP"
    [ "${1:-}" = "--sync" ] && exit 0
fi

# dependencies are handled above, so skip ModuleUpdate's slower check
export SKIP_REQUIREMENTS_UPDATE=1
# PYTHONPATH rather than cd, so relative paths in args (patch files) still work from any folder
PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" exec "$PY" -m aplite "$@"
