"""Opening files, folders and links with the desktop's default app, without its chatter landing in our terminal."""
from __future__ import annotations

import subprocess
import sys
import typing
from shutil import which


def open_path(target: str) -> bool:
    """Open a file, folder or URL; False if there's no desktop opener (e.g. a terminal-only machine, or no app for
    that file type on Windows)."""
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
    try:
        os.startfile(target)  # type: ignore[attr-defined]  # windows
    except OSError:
        return False
    return True


def open_link(widget: typing.Any, url: str) -> None:
    """Open a page in the desktop's browser; without one, a notification shows the link to open by hand."""
    if not open_path(url):
        widget.notify(f"No desktop browser to open it; open {url}", timeout=10)
