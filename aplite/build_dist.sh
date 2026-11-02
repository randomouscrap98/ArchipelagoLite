#!/bin/sh
# Builds dist/ArchipelagoLite_<version>_x86_64.AppImage and dist/ArchipelagoLite_<version>_linux-x86_64.tar.gz:
# a relocatable glibc Python (python-build-standalone, via uv) + aplite's dependencies + the source tree. No freezing.
# Packages are cross-installed as manylinux wheels and nothing built is run, so this also works on musl hosts.
# Needs uv, curl and tar; mksquashfs (squashfs-tools) for the AppImage (without it only the tar.gz is built).
# Only git-tracked files are packaged (working tree contents).
# Env: MKSQUASHFS (mksquashfs command), APLITE_RUNTIME_URL (AppImage type2 runtime to download)
set -eu

REPO=$(cd "$(dirname "$0")/.." && pwd)
PYVER=3.13
PLATFORM=x86_64-manylinux_2_28
RUNTIME_URL=${APLITE_RUNTIME_URL:-https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64}
MKSQUASHFS=${MKSQUASHFS:-mksquashfs}
BUILD="$REPO/build/aplite-dist"
APPDIR="$BUILD/ArchipelagoLite"
DIST="$REPO/dist"
EXCLUDE='^(\.github|\.run|test|deploy|typings)/|^\.'  # git-tracked paths left out of the package

PATH="$PATH:$HOME/.local/bin"
for tool in uv curl tar git; do
    command -v "$tool" >/dev/null 2>&1 || { echo "build_dist: needs $tool" >&2; exit 1; }
done
appimage=1
command -v "$MKSQUASHFS" >/dev/null 2>&1 || { echo "build_dist: no mksquashfs, building the tar.gz only" >&2; appimage=0; }

version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO/Utils.py")
name="ArchipelagoLite_${version}-$(git -C "$REPO" rev-parse --short HEAD)"

rm -rf "$BUILD"
mkdir -p "$APPDIR/app" "$DIST"

echo "build_dist: python"
uv python install --quiet --no-bin --install-dir "$BUILD/python-dl" "cpython-$PYVER-linux-x86_64-gnu"
cp -a "$(ls -d "$BUILD"/python-dl/cpython-$PYVER.*-linux-x86_64-gnu | head -1)" "$APPDIR/python"
lib="$APPDIR/python/lib/python$PYVER"
rm -rf "$APPDIR/python/include" "$APPDIR/python/share" "$lib/idlelib" "$lib/turtledemo" "$lib/ensurepip" \
    "$lib"/config-$PYVER-*

echo "build_dist: dependencies"
install() {
    uv pip install --quiet --target "$lib/site-packages" --python-platform "$PLATFORM" --python-version "$PYVER" "$@"
}
uv run --quiet --no-project --python "$PYVER" python "$REPO/aplite/requirements.py" "$REPO/requirements.txt" \
    > "$BUILD/requirements-lite.txt"
install --only-binary :all: -r "$BUILD/requirements-lite.txt" -r "$REPO/aplite/requirements.txt"
# built-in world requirements, one by one when a file fails as a whole (same as aplite.sh). Packages without a
# wheel are built from source: fine for pure Python ones, a compiled one would only run on glibcs like this host's
for r in "$REPO"/worlds/*/requirements.txt; do
    install --only-binary :all: -r "$r" 2>/dev/null || {
        grep -v -e 'hash=' -e '^[[:space:]]*#' "$r" | sed 's/[ \\]*$//' | while read -r req; do
            [ -n "$req" ] || continue
            install --only-binary :all: "$req" 2>/dev/null && continue
            if install "$req" 2>/dev/null; then
                echo "build_dist: note: built $req from source ($r)" >&2
            else
                echo "build_dist: WARNING: could not install $req ($r)" >&2
            fi
        done
    }
done

echo "build_dist: source"
git -C "$REPO" ls-files | grep -Ev "$EXCLUDE" | while read -r f; do
    [ -e "$REPO/$f" ] && echo "$f"
done > "$BUILD/files.txt"
tar -C "$REPO" -cf - -T "$BUILD/files.txt" | tar -C "$APPDIR/app" -xf -
# untracked, but Utils.user_path copies it to the home folder (templates: "Generate Template Options" in the launcher)
mkdir -p "$APPDIR/app/Players/Templates"
cp "$REPO/meta.yaml" "$APPDIR/app/Players/Templates/"
# Utils.user_path copies Players/sprites/lua to the home folder when this differs from the copy there
printf '{"buildtime": "%s", "hashes": {}, "version": [%s]}\n' "$(date -u '+%Y-%m-%d %H:%M:%S')" \
    "$(echo "$version" | sed 's/\./, /g')" > "$APPDIR/app/manifest.json"

echo "build_dist: bytecode"
# the AppImage is read-only, so compile now (bytecode doesn't depend on the platform, only the Python version)
uv run --quiet --no-project --python "$PYVER" python -m compileall -q -j0 "$lib" "$APPDIR/app" >/dev/null

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE=$(dirname "$(readlink -f "$0")")
export SKIP_REQUIREMENTS_UPDATE=1
export PYTHONPATH="$HERE/app${PYTHONPATH:+:$PYTHONPATH}"
exec "$HERE/python/bin/python3" -m aplite "$@"
EOF
chmod +x "$APPDIR/AppRun"
ln -s AppRun "$APPDIR/aplite"
cp "$REPO/data/icon.png" "$APPDIR/archipelago-lite.png"
ln -s archipelago-lite.png "$APPDIR/.DirIcon"
cat > "$APPDIR/archipelago-lite.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Archipelago Lite
Comment=Terminal frontend for Archipelago
Exec=aplite
Icon=archipelago-lite
Terminal=true
Categories=Game;
EOF

echo "build_dist: packing"
tar -C "$BUILD" -czf "$DIST/${name}_linux-x86_64.tar.gz" ArchipelagoLite
echo "  $DIST/${name}_linux-x86_64.tar.gz"
if [ "$appimage" = 1 ]; then
    runtime="$REPO/build/appimage-runtime-x86_64"
    [ -f "$runtime" ] || curl -fsSL -o "$runtime" "$RUNTIME_URL"
    "$MKSQUASHFS" "$APPDIR" "$BUILD/app.squashfs" -root-owned -noappend -comp zstd -quiet -no-progress
    cat "$runtime" "$BUILD/app.squashfs" > "$DIST/${name}_x86_64.AppImage"
    chmod +x "$DIST/${name}_x86_64.AppImage"
    echo "  $DIST/${name}_x86_64.AppImage"
fi
