"""
Linux tar.gz and Windows zip: user data in the top folder (next to app/), laid out like upstream's tar.gz / Windows
builds keep it next to their launcher, so Lite can be extracted into an existing Archipelago folder and share
host.yaml, Players, custom_worlds and output with it. The tar.gz's aplite script and aplite.exe set APLITE_DATA_DIR; the AppImage
and source checkouts don't.

Lite versions before this kept user data in app/; the first run moves it up (see migrate).
"""
import dataclasses
import filecmp
import logging
import os
import shutil

ENV = "APLITE_DATA_DIR"
SHIPPED = os.path.join("aplite", "_files.txt")
"""In app/: the files the build shipped (build_dist.sh). Anything else in app/ is user data."""
MARKER = ".aplite-manifest.json"
"""In the data folder: copy of app/manifest.json once populated (upstream's own manifest.json there is left alone)."""


@dataclasses.dataclass
class Migration:
    source: str
    target: str
    moved: int = 0
    skipped: list[str] = dataclasses.field(default_factory=list)
    """Relative paths left in app/: the same name already existed in the data folder, or the move failed."""


migration: Migration | None = None
"""Set by install() when this process moved user data out of app/ (the launcher shows it)."""


def install() -> None:
    """First-run move and template copy for APLITE_DATA_DIR. Before anything reads user data (host.yaml, worlds).
    Utils.user_path itself picks APLITE_DATA_DIR (core edit), so processes that never import aplite agree."""
    global migration
    import Utils
    data = os.path.abspath(os.environ[ENV])
    app = Utils.local_path()
    if not os.path.exists(os.path.join(data, MARKER)):
        migration = migrate(app, data)
        if migration.moved or migration.skipped:
            logging.info(f"Moved {migration.moved} files from {app} to {data}; left: {migration.skipped}")
        else:
            migration = None
    populate(app, data)


def populate(app: str, data: str) -> None:
    """What Utils.user_path does for a home folder: copy Players (templates), sprites and lua when app/ changed."""
    manifest, marker = os.path.join(app, "manifest.json"), os.path.join(data, MARKER)
    if os.path.exists(marker) and os.path.exists(manifest) and filecmp.cmp(manifest, marker, shallow=True):
        return
    try:
        for folder in ("Players", "data/sprites", "data/lua"):
            if os.path.isdir(os.path.join(app, folder)):
                shutil.copytree(os.path.join(app, folder), os.path.join(data, folder), dirs_exist_ok=True)
        if os.path.exists(manifest):
            shutil.copy2(manifest, marker)
    except OSError as e:
        logging.warning(f"Could not copy templates/sprites/lua to {data}: {e}")


def migrate(app: str, data: str) -> Migration:
    """Move every file in app/ the build didn't ship to the same place under `data`. Never overwrites."""
    result = Migration(app, data)
    try:
        with open(os.path.join(app, SHIPPED), encoding="utf-8") as f:
            shipped = {line.rstrip("\n") for line in f}
    except OSError:  # not a tar.gz build: nothing tells user data from ours
        return result
    shipped.add(SHIPPED.replace(os.sep, "/"))
    touched = set()
    worlds = os.path.join(app, "worlds")
    for root, dirs, files in os.walk(app):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        # a symlinked folder (os.walk doesn't enter it) moves as one entry
        links = [d for d in dirs if os.path.islink(os.path.join(root, d))]
        dirs[:] = [d for d in dirs if d not in links]
        for name in files + links:
            source = os.path.join(root, name)
            rel = os.path.relpath(source, app).replace(os.sep, "/")
            if rel in shipped:
                continue
            # an .apworld dropped into app/worlds loaded as a world; in the data folder only custom_worlds does
            moved_rel = f"custom_worlds/{name}" if root == worlds and name.endswith(".apworld") else rel
            target = os.path.join(data, moved_rel)
            if os.path.lexists(target):
                result.skipped.append(rel)
                continue
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.rename(source, target)
            except OSError as e:
                logging.warning(f"Could not move {source} to {target}: {e}")
                result.skipped.append(rel)
                continue
            result.moved += 1
            touched.add(root)
    for folder in sorted(touched, key=len, reverse=True):  # deepest first: folders the move emptied
        while folder != app and os.path.isdir(folder) and not os.listdir(folder):
            os.rmdir(folder)
            folder = os.path.dirname(folder)
    return result


def message(m: Migration) -> str:
    text = (f"Archipelago Lite now keeps your files (host.yaml, Players, custom_worlds, output, ROMs…) in\n"
            f"  {m.target}\ninstead of\n  {m.source}\n\nMoved {m.moved} files there. "
            "To update Lite later, replace app/ and python/: your files stay.")
    if m.skipped:
        shown = "\n".join(f"  {p}" for p in m.skipped[:10])
        more = f"\n  …and {len(m.skipped) - 10} more" if len(m.skipped) > 10 else ""
        text += (f"\n\nLeft {len(m.skipped)} in app/ (a file with that name already exists there, or the move "
                 f"failed; see the log):\n{shown}{more}")
    return text
