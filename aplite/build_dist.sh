#!/bin/sh
# Builds into dist/, from a relocatable Python (python-build-standalone, via uv) + aplite's dependencies + the source
# tree (no freezing):
#   ArchipelagoLite_<version>_x86_64.AppImage and _linux-x86_64.tar.gz (one AppDir, glibc Python)
#   ArchipelagoLite_<version>_windows-x86_64.zip (run aplite.cmd)
# Packages are cross-installed as platform wheels and nothing built is run, so this works on any Linux (musl too).
# Needs uv, curl and tar; mksquashfs (squashfs-tools) for the AppImage (without it that one is skipped).
# Only git-tracked files are packaged (working tree contents).
# Env: MKSQUASHFS (mksquashfs command), APLITE_RUNTIME_URL (AppImage type2 runtime to download)
set -eu

REPO=$(cd "$(dirname "$0")/.." && pwd)
PYVER=3.13
RUNTIME_URL=${APLITE_RUNTIME_URL:-https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64}
MKSQUASHFS=${MKSQUASHFS:-mksquashfs}
BUILD="$REPO/build/aplite-dist"
DIST="$REPO/dist"
EXCLUDE='^(\.github|\.run|test|deploy|typings)/|^\.'  # git-tracked paths left out of the package

PATH="$PATH:$HOME/.local/bin"
for tool in uv curl tar git; do
    command -v "$tool" >/dev/null 2>&1 || { echo "build_dist: needs $tool" >&2; exit 1; }
done
appimage=1
command -v "$MKSQUASHFS" >/dev/null 2>&1 || { echo "build_dist: no mksquashfs, skipping the AppImage" >&2; appimage=0; }

version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO/Utils.py")
name="ArchipelagoLite_${version}-$(git -C "$REPO" rev-parse --short HEAD)"
host_python() { uv run --quiet --no-project --python "$PYVER" python "$@"; }

rm -rf "$BUILD"
mkdir -p "$BUILD/app" "$DIST"

echo "build_dist: source"
git -C "$REPO" ls-files | grep -Ev "$EXCLUDE" | while read -r f; do
    [ -e "$REPO/$f" ] && echo "$f"
done > "$BUILD/files.txt"
tar -C "$REPO" -cf - -T "$BUILD/files.txt" | tar -C "$BUILD/app" -xf -
# untracked, but Utils.user_path copies it to the home folder (templates: "Generate Template Options" in the launcher)
mkdir -p "$BUILD/app/Players/Templates"
cp "$REPO/meta.yaml" "$BUILD/app/Players/Templates/"
# Utils.user_path copies Players/sprites/lua to the home folder when this differs from the copy there
printf '{"buildtime": "%s", "hashes": {}, "version": [%s]}\n' "$(date -u '+%Y-%m-%d %H:%M:%S')" \
    "$(echo "$version" | sed 's/\./, /g')" > "$BUILD/app/manifest.json"
host_python "$REPO/aplite/requirements.py" "$REPO/requirements.txt" > "$BUILD/requirements-lite.txt"

# python_tree DEST UV_PYTHON SITE_PACKAGES WHEEL_PLATFORM: Python + dependencies + source + bytecode in DEST
python_tree() {
    dest=$1 site=$1/python/$3 platform=$4
    echo "build_dist: $2"
    uv python install --quiet --no-bin --install-dir "$BUILD/python-dl" "$2"
    mkdir -p "$dest"
    # the versioned folder (uv also makes an unversioned link to it)
    cp -a "$(ls -d "$BUILD"/python-dl/$(echo "$2" | sed "s/-$PYVER-/-$PYVER.*-/") | head -1)" "$dest/python"
    install() {
        uv pip install --quiet --target "$site" --python-platform "$platform" --python-version "$PYVER" "$@"
    }
    install --only-binary :all: -r "$BUILD/requirements-lite.txt" -r "$REPO/aplite/requirements.txt"
    # built-in world requirements, one by one when a file fails as a whole (same as aplite.sh). Packages without a
    # wheel are built from source: fine for pure Python ones, a compiled one would be built for this host instead
    for r in "$REPO"/worlds/*/requirements.txt; do
        install --only-binary :all: -r "$r" 2>/dev/null || {
            grep -v -e 'hash=' -e '^[[:space:]]*#' "$r" | sed 's/[ \\]*$//' | while read -r req; do
                [ -n "$req" ] || continue
                install --only-binary :all: "$req" 2>/dev/null && continue
                if install "$req" 2>/dev/null; then
                    echo "build_dist: note: built $req from source ($r)" >&2
                else
                    echo "build_dist: WARNING: could not install $req for $platform ($r)" >&2
                fi
            done
        }
    done
    cp -a "$BUILD/app" "$dest/app"
    # the AppImage is read-only, so compile now (bytecode doesn't depend on the platform, only the Python version)
    host_python -m compileall -q -j0 "$site/.." "$dest/app" >/dev/null
}

# Linux: AppImage + tar.gz
APPDIR="$BUILD/linux/ArchipelagoLite"
python_tree "$APPDIR" "cpython-$PYVER-linux-x86_64-gnu" "lib/python$PYVER/site-packages" x86_64-manylinux_2_28
lib="$APPDIR/python/lib/python$PYVER"
rm -rf "$APPDIR/python/include" "$APPDIR/python/share" "$lib/idlelib" "$lib/turtledemo" "$lib/ensurepip" \
    "$lib"/config-$PYVER-*
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
tar -C "$BUILD/linux" -czf "$DIST/${name}_linux-x86_64.tar.gz" ArchipelagoLite
echo "  $DIST/${name}_linux-x86_64.tar.gz"
if [ "$appimage" = 1 ]; then
    runtime="$REPO/build/appimage-runtime-x86_64"
    [ -f "$runtime" ] || curl -fsSL -o "$runtime" "$RUNTIME_URL"
    "$MKSQUASHFS" "$APPDIR" "$BUILD/app.squashfs" -root-owned -noappend -comp zstd -quiet -no-progress
    cat "$runtime" "$BUILD/app.squashfs" > "$DIST/${name}_x86_64.AppImage"
    chmod +x "$DIST/${name}_x86_64.AppImage"
    echo "  $DIST/${name}_x86_64.AppImage"
fi

# Windows: zip
WINDIR="$BUILD/windows/ArchipelagoLite"
python_tree "$WINDIR" "cpython-$PYVER-windows-x86_64-none" Lib/site-packages x86_64-pc-windows-msvc
rm -rf "$WINDIR/python/include" "$WINDIR/python/libs" "$WINDIR/python/Lib/idlelib" "$WINDIR/python/Lib/turtledemo" \
    "$WINDIR/python/Lib/ensurepip"
# CRLF: cmd misreads labels in LF-only batch files
awk '{ printf "%s\r\n", $0 }' > "$WINDIR/aplite.cmd" <<'EOF'
@echo off
rem Archipelago Lite with the bundled Python. Prefers Windows Terminal; set APLITE_NO_WT=1 to stay in this console.
setlocal
if not defined APLITE_NO_WT if not defined WT_SESSION (
    where wt >nul 2>nul && (
        rem "call" first: cmd /c strips the outer quotes of a line that starts with one
        start "" wt -d . cmd /c call "%~f0" %*
        exit /b 0
    )
)
set "HERE=%~dp0"
set SKIP_REQUIREMENTS_UPDATE=1
set "PYTHONPATH=%HERE%app;%PYTHONPATH%"
rem aplite exits with this code to be started again, fresh (coming back to the launcher)
set APLITE_RELAUNCH=75
"%HERE%python\python.exe" -m aplite %*
:again
if not "%ERRORLEVEL%"=="75" exit /b %ERRORLEVEL%
"%HERE%python\python.exe" -m aplite
goto again
EOF
rm -f "$DIST/${name}_windows-x86_64.zip"
host_python - "$BUILD/windows" "$DIST/${name}_windows-x86_64.zip" <<'EOF'
import os, sys, zipfile
root, out = sys.argv[1:]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for folder, _, files in os.walk(root):
        for f in files:
            path = os.path.join(folder, f)
            if not os.path.islink(path):  # uv's unix-style python -> python.exe link; zips for Windows can't hold links
                z.write(path, os.path.relpath(path, root))
EOF
echo "  $DIST/${name}_windows-x86_64.zip"
