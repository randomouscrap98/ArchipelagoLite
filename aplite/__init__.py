"""
ArchipelagoLite: terminal frontend for Archipelago clients.

Run with `python -m aplite [Patch|Game|Component|url] [component args]` (or ./aplite.sh, which sets up the venv).
"""
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
"""The Archipelago checkout aplite lives in."""


def child_env() -> dict[str, str]:
    """Environment for Python processes aplite starts (relaunches, command-line scripts): the repo importable even
    when aplite was started from another folder."""
    pythonpath = os.pathsep.join(filter(None, [REPO, os.environ.get("PYTHONPATH")]))
    return {**os.environ, "PYTHONPATH": pythonpath}
