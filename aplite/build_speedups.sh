#!/bin/sh
# Builds _speedups (NetUtils' compiled LocationStore) without a system compiler: Cython and zig from PyPI via uvx.
# Without it NetUtils falls back to pyximport, which builds it on every launch (or fails there without a compiler).
# Usage: build_speedups.sh OUT PYTHON_INCLUDE_DIR [ZIG_TARGET] [LINK_ARGS...]
#   ZIG_TARGET empty = this machine; e.g. x86_64-linux-gnu.2.17, x86_64-windows-gnu (then LINK_ARGS: python313.lib)
set -eu
REPO=$(cd "$(dirname "$0")/.." && pwd)
CYTHON=cython==3.2.4  # same as requirements.txt
CYMEM=cymem==2.0.13
ZIG=ziglang==0.17.1
out=$1 include=$2 target=${3:-}
shift 2
[ $# -gt 0 ] && shift
PATH="$PATH:$HOME/.local/bin"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
uvx --quiet --from "$CYTHON" --with "$CYMEM" cython -3 "$REPO/_speedups.pyx" -o "$tmp/_speedups.c"
uvx --quiet --from "$ZIG" python-zig cc ${target:+-target "$target"} -shared -fPIC -O2 -s -I"$REPO" -I"$include" \
    "$tmp/_speedups.c" "$@" -o "$tmp/$(basename "$out")"
mv "$tmp/$(basename "$out")" "$out"  # only the module: zig also writes .lib/.pdb next to a Windows one
