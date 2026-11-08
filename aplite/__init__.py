"""
ArchipelagoLite: terminal frontend for Archipelago clients.

Run with `python -m aplite [Patch|Game|Component|url] [component args]` (or ./aplite.sh, which sets up the venv).
"""
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
"""The Archipelago checkout aplite lives in."""
OFFICIAL_SITE = "https://archipelago.gg"


def write_atomic(path: str, data: str | bytes) -> None:
    """Write a whole file so readers (other aplite processes, the self-host server) never see half of it. Creates the
    folder; raises OSError."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"  # per process: several may write at once
    with open(tmp, "wb") as f:
        f.write(data.encode("utf-8") if isinstance(data, str) else data)
    os.replace(tmp, path)


def child_env() -> dict[str, str]:
    """Environment for Python processes aplite starts (relaunches, scripts, the file picker): the repo importable even
    when aplite was started from another folder, and no ModuleUpdate check (aplite handled requirements already, and
    upstream's would include kivy)."""
    pythonpath = os.pathsep.join(filter(None, [REPO, os.environ.get("PYTHONPATH")]))
    return {**os.environ, "PYTHONPATH": pythonpath, "SKIP_REQUIREMENTS_UPDATE": "1"}


def wait_for_enter(message: str = "Press Enter to return to the launcher…") -> None:
    """Keep terminal output on screen until the user is done reading it."""
    try:
        input(f"\n{message}")
    except EOFError:
        pass


if os.environ.get("APLITE_DATA_DIR"):  # tar.gz / Windows zip (aplite script, aplite.exe): data next to app/; every aplite process
    from aplite import datadir
    datadir.install()
