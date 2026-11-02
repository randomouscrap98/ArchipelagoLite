"""Opening files, folders and links with the desktop's default app, without its chatter landing in our terminal."""
from __future__ import annotations

import subprocess
import sys
from shutil import which


def open_path(target: str) -> bool:
    """Open a file, folder or URL; False if there's no desktop opener (e.g. a terminal-only machine)."""
    if sys.platform.startswith("linux"):
        opener = which("xdg-open") or which("gnome-open") or which("kde-open")
        if not opener:
            return False
        subprocess.Popen([opener, target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    if sys.platform == "darwin":
        subprocess.Popen(["open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    import os
    os.startfile(target)  # type: ignore[attr-defined]  # windows
    return True
