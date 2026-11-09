#!/bin/sh
# Builds into dist/, from a relocatable Python (python-build-standalone, via uv) + aplite's dependencies + the source
# tree (no freezing):
#   ArchipelagoLite_<version>_x86_64.AppImage and _linux-x86_64.tar.gz (one AppDir, glibc Python)
#   ArchipelagoLite_<version>_windows-x86_64.zip (run aplite.exe)
# Packages are cross-installed as platform wheels and nothing built is run, so this works on any Linux (musl too).
# _speedups is compiled with zig (aplite/build_speedups.sh, via uvx: no system compiler).
# Needs uv, curl and tar; mksquashfs (squashfs-tools) for the AppImage (without it that one is skipped).
# Only git-tracked files are packaged (working tree contents).
# Env: MKSQUASHFS (mksquashfs command), APLITE_RUNTIME_URL (AppImage type2 runtime to download)
set -eu

REPO=$(cd "$(dirname "$0")/.." && pwd)
PYVER=3.13
RUNTIME_URL=${APLITE_RUNTIME_URL:-https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64}
MKSQUASHFS=${MKSQUASHFS:-mksquashfs}
. "$REPO/aplite/build_pins.sh"  # ZIG
SNI_VERSION=v0.0.100  # same as upstream's setup.py (SNI_VERSION)
# Microsoft's VC++ runtime DLLs (msvcp140...) from the msvc-runtime wheel: fresh Windows lacks them, and C++ extensions
# (dolphin_memory_engine: Luigi's Mansion, TWW, ...) fail to load. Upstream's installer runs vc_redist.x64.exe instead.
VCRT_WHEEL=https://files.pythonhosted.org/packages/c2/6e/7ba5696e9d8c1bcb01352f06e441497c308e84f75aba598400646ab31ca7/msvc_runtime-14.51.36231-cp313-cp313-win_amd64.whl
VCRT_SHA256=f3c3ac4f733a774f6ae209567e533bfde6f72dc1c10977733bfe09ce01e5b624
BUILD="$REPO/build/aplite-dist"
DIST="$REPO/dist"
EXCLUDE='^(\.github|\.run|test|deploy|typings)/|^\.'  # git-tracked paths left out of the package

PATH="$PATH:$HOME/.local/bin"
for tool in uv curl tar git; do
    command -v "$tool" >/dev/null 2>&1 || { echo "build_dist: needs $tool" >&2; exit 1; }
done
appimage=1
command -v "$MKSQUASHFS" >/dev/null 2>&1 || { echo "build_dist: no mksquashfs, skipping the AppImage" >&2; appimage=0; }

host_python() { uv run --quiet --no-project --python "$PYVER" python "$@"; }
version=$(PYTHONPATH="$REPO" host_python -m aplite.version --upstream)
aplite_version=$(PYTHONPATH="$REPO" host_python -m aplite.version)
name="ArchipelagoLite_${aplite_version}"

rm -rf "$BUILD"
mkdir -p "$BUILD/app" "$DIST"

echo "build_dist: source"
git -C "$REPO" ls-files | grep -Ev "$EXCLUDE" | while read -r f; do
    [ -e "$REPO/$f" ] && echo "$f"
done > "$BUILD/files.txt"
tar -C "$REPO" -cf - -T "$BUILD/files.txt" | tar -C "$BUILD/app" -xf -
echo "$aplite_version" > "$BUILD/app/aplite/_version.txt"  # no git in the package (aplite/version.py)
# Players/Templates is untracked, but Utils.user_path copies it to the home folder (templates: "Generate Template
# Options" in the launcher)
mkdir -p "$BUILD/app/Players/Templates"
mv "$BUILD/app/meta.yaml" "$BUILD/app/Players/Templates/"  # upstream's root copy: nothing reads it there
# Utils.user_path copies Players/sprites/lua to the home folder when this differs from the copy there
printf '{"buildtime": "%s", "hashes": {}, "version": [%s]}\n' "$(date -u '+%Y-%m-%d %H:%M:%S')" \
    "$(echo "$version" | sed 's/\./, /g')" > "$BUILD/app/manifest.json"
# core worlds as .apworlds, like official builds (setup.py): far fewer files to open (aplite/pack_worlds.py)
host_python "$REPO/aplite/pack_worlds.py" "$BUILD/app" "$version"
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
            host_python "$REPO/aplite/requirements.py" --each "$r" | while read -r req; do
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
    # the AppImage is read-only, so compile now (bytecode doesn't depend on the platform, only the Python version).
    # Hash-based: a timestamp .pyc never matches after unzipping (zip stores 2-second, timezone-less times), so Windows
    # recompiled all ~850 on first start. Unchecked: the source is never read; app/ is replaced as a whole on update.
    host_python -m compileall -q -j0 --invalidation-mode unchecked-hash "$site/.." "$dest/app" >/dev/null
}

# sni DEST ASSET: SNI (SNES clients' bridge to emulators/hardware; host.yaml sni_path "SNI" finds it) in DEST/app/SNI,
# as upstream's builds bundle it. Downloads are kept in build/sni-dl.
sni() {
    archive="$REPO/build/sni-dl/sni-$SNI_VERSION-$2"
    mkdir -p "$REPO/build/sni-dl"
    if [ ! -f "$archive" ]; then
        curl -fsSL -o "$archive.part" "https://github.com/alttpo/sni/releases/download/$SNI_VERSION/sni-$SNI_VERSION-$2"
        mv "$archive.part" "$archive"
    fi
    host_python - "$archive" "$1/app/SNI" <<'EOF'
import os, shutil, sys, tarfile, tempfile, zipfile
archive, dest = sys.argv[1:]
with tempfile.TemporaryDirectory() as tmp:
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(tmp)
    else:
        with tarfile.open(archive) as t:
            t.extractall(tmp, filter="tar")
    entries = os.listdir(tmp)
    only = os.path.join(tmp, entries[0]) if len(entries) == 1 else ""
    shutil.copytree(only if os.path.isdir(only) else tmp, dest)  # the Linux tarball has a top folder, the zip not
EOF
}

# speedups DEST MODULE ZIG_TARGET [LINK_ARGS...]: compiled _speedups in DEST/app (aplite/build_speedups.sh), from the
# Python headers in DEST/python. The .pyx stays: NetUtils' pyximport fallback if the module ever fails to load.
speedups() {
    dest=$1 module=$2
    shift 2
    echo "build_dist: _speedups ($1)"
    include=$(ls -d "$dest/python/include/python$PYVER" 2>/dev/null || echo "$dest/python/include")
    sh "$REPO/aplite/build_speedups.sh" "$dest/app/$module" "$include" "$@"
}

# vcruntime DEST: the msvc-runtime wheel's DLLs (what it would put in sys.prefix) next to DEST/python/python.exe, and
# its license. Download kept in build/vcrt-dl.
vcruntime() {
    wheel="$REPO/build/vcrt-dl/$(basename "$VCRT_WHEEL")"
    mkdir -p "$REPO/build/vcrt-dl"
    if [ ! -f "$wheel" ]; then
        curl -fsSL -o "$wheel.part" "$VCRT_WHEEL"
        mv "$wheel.part" "$wheel"
    fi
    [ "$(sha256sum "$wheel" | cut -d' ' -f1)" = "$VCRT_SHA256" ] || { echo "build_dist: bad msvc-runtime wheel" >&2; exit 1; }
    host_python - "$wheel" "$1/python" <<'EOF'
import os, sys, zipfile
wheel, dest = sys.argv[1:]
with zipfile.ZipFile(wheel) as z:
    for name in z.namelist():
        parts = name.split("/")
        if len(parts) == 3 and parts[0].endswith(".data") and parts[1] == "data" and parts[2].endswith(".dll"):
            with open(os.path.join(dest, parts[2]), "wb") as f:
                f.write(z.read(name))
        elif name.endswith("/licenses/LICENSE"):
            with open(os.path.join(dest, "msvc-runtime-LICENSE.txt"), "wb") as f:
                f.write(z.read(name))
EOF
}

# shipped DEST: what DEST/app holds, in app/aplite/_files.txt; anything else there is user data from older versions
# (aplite/datadir.py moves it up to the data folder)
shipped() {
    (cd "$1/app" && find . -path '*/__pycache__' -prune -o \( -type f -o -type l \) -print | sed 's|^\./||') \
        > "$BUILD/files-shipped.txt"
    mv "$BUILD/files-shipped.txt" "$1/app/aplite/_files.txt"
}

# Linux: AppImage + tar.gz
APPDIR="$BUILD/linux/ArchipelagoLite"
python_tree "$APPDIR" "cpython-$PYVER-linux-x86_64-gnu" "lib/python$PYVER/site-packages" x86_64-manylinux_2_28
speedups "$APPDIR" "_speedups.cpython-$(echo $PYVER | tr -d .)-x86_64-linux-gnu.so" x86_64-linux-gnu.2.17
sni "$APPDIR" manylinux_2_28-amd64.tar.xz
shipped "$APPDIR"
lib="$APPDIR/python/lib/python$PYVER"
rm -rf "$APPDIR/python/include" "$APPDIR/python/share" "$lib/idlelib" "$lib/turtledemo" "$lib/ensurepip" \
    "$lib"/config-$PYVER-*
cp "$REPO/aplite/dist-launcher.sh" "$APPDIR/aplite"  # also reopens itself in a terminal when double-clicked
chmod +x "$APPDIR/aplite"
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
    ln -s aplite "$APPDIR/AppRun"  # the AppImage's entry point; not in the tar.gz (users wondered what it was)
    "$MKSQUASHFS" "$APPDIR" "$BUILD/app.squashfs" -root-owned -noappend -comp zstd -quiet -no-progress
    cat "$runtime" "$BUILD/app.squashfs" > "$DIST/${name}_x86_64.AppImage"
    chmod +x "$DIST/${name}_x86_64.AppImage"
    rm "$APPDIR/AppRun"
    echo "  $DIST/${name}_x86_64.AppImage"
fi

# Windows: zip
WINDIR="$BUILD/windows/ArchipelagoLite"
python_tree "$WINDIR" "cpython-$PYVER-windows-x86_64-none" Lib/site-packages x86_64-pc-windows-msvc
speedups "$WINDIR" "_speedups.cp$(echo $PYVER | tr -d .)-win_amd64.pyd" x86_64-windows-gnu \
    "$WINDIR/python/libs/python$(echo $PYVER | tr -d .).lib"
vcruntime "$WINDIR"
sni "$WINDIR" windows-amd64.zip
shipped "$WINDIR"
rm -rf "$WINDIR/python/include" "$WINDIR/python/libs" "$WINDIR/python/Lib/idlelib" "$WINDIR/python/Lib/turtledemo" \
    "$WINDIR/python/Lib/ensurepip"
# the Windows launcher: aplite.exe (aplite/launcher_win.c, zig like _speedups, with the icon), and
# aplite_fallback.cmd in case antivirus/SmartScreen blocks the unsigned exe (CRLF: cmd misreads labels in LF-only
# batch files)
printf '1 ICON "%s"\n' "$REPO/data/icon.ico" > "$BUILD/aplite.rc"
(cd "$BUILD" && uvx --quiet --from "$ZIG" python-zig cc -target x86_64-windows-gnu -municode -O2 -s \
    -o "$WINDIR/aplite.exe" "$REPO/aplite/launcher_win.c" aplite.rc)  # zig's rc failed (FileSystemFailure) when not run from the .rc's folder
awk '{ printf "%s\r\n", $0 }' "$REPO/aplite/aplite_fallback.cmd" > "$WINDIR/aplite_fallback.cmd"
rm -f "$DIST/${name}_windows-x86_64.zip"
# flat (no top folder): extracting into an Archipelago folder puts aplite.exe next to its launcher, sharing its data;
# Windows' Extract All still makes a folder of its own by default
host_python - "$WINDIR" "$DIST/${name}_windows-x86_64.zip" <<'EOF'
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
