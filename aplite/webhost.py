"""
Host on Archipelago: upload a generated game (output .zip / .archipelago) to the website (pref "webhost_url", default
archipelago.gg) and open its seed page, where the user creates the room in their own browser (so they own it and get
its server console). Used by the launcher entry and by Generate GUI after a generate.
"""
from __future__ import annotations

import html
import os
import re
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Label, LoadingIndicator

import Utils
from aplite import prefs
from aplite.nav import AppliteApp

SUFFIXES = (".zip", ".archipelago")
"""Generated games (Generate's output zip, or a bare multidata)."""
TIMEOUT = (15, 600)
"""Seconds to connect, and to wait for the site's answer (it reads the whole multidata before replying)."""


class UploadError(Exception):
    pass


def site() -> str:
    return str(prefs.get("webhost_url")).rstrip("/")


def upload(path: Path) -> str:
    """Upload a generated game; returns its seed page URL. The site answers a good upload with a redirect to
    /seed/<id>, a bad one with its upload page again carrying the reason as a flashed message."""
    import requests
    try:
        with open(path, "rb") as f:
            response = requests.post(f"{site()}/uploads", files={"file": (path.name, f)}, timeout=TIMEOUT)
    except (OSError, requests.RequestException) as e:
        raise UploadError(str(e)) from e
    if "/seed/" in response.url:
        return response.url
    if response.status_code == 413:
        raise UploadError("the file is too big for the site")
    messages = [html.unescape(re.sub(r"<[^>]+>", "", m)).strip()
                for m in re.findall(r'<div class="user-message">(.*?)</div>', response.text, re.S)]
    raise UploadError("; ".join(messages) or f"unexpected answer from the site (HTTP {response.status_code})")


class UploadModal(ModalScreen[None]):
    """Uploads `path`, then opens the seed page (or shows its URL); can't be closed while uploading."""
    CSS = """
    UploadModal { align: center middle; }
    #upload-box { width: 80%; height: auto; padding: 1 2; border: thick $primary; background: $surface; }
    #upload-box Label { width: 100%; }
    LoadingIndicator { height: 1; }
    #upload-buttons { height: auto; align-horizontal: right; }
    """
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.busy = True

    def compose(self) -> ComposeResult:
        with Vertical(id="upload-box"):
            yield Label(f"Uploading {self.path.name} to {site()}…", id="upload-status")
            yield LoadingIndicator()
            yield Label("", id="upload-url")
            with Horizontal(id="upload-buttons"):
                yield Button("Close", id="upload-close", disabled=True)

    def on_mount(self) -> None:
        self.run_worker(self.run_upload, thread=True)

    def run_upload(self) -> None:
        try:
            url, error = upload(self.path), None
        except UploadError as e:
            url, error = None, str(e)
        self.app.call_from_thread(self.done, url, error)

    def done(self, url: str | None, error: str | None) -> None:
        from aplite.desktop import open_path
        self.busy = False
        self.query_one(LoadingIndicator).display = False
        status = self.query_one("#upload-status", Label)
        if url is None:
            status.update(f"Upload failed: {error}")
            status.styles.color = "red"
        elif open_path(url):
            status.update("Uploaded. Opened the seed page in your browser: click Create New Room there.")
        else:
            status.update("Uploaded. Open this page in a browser and click Create New Room:")
        self.query_one("#upload-url", Label).update(url or "")
        close = self.query_one("#upload-close", Button)
        close.disabled = False
        close.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_close()

    def action_close(self) -> None:
        if not self.busy:
            self.dismiss(None)


class HostApp(AppliteApp):
    """The launcher entry: just the upload dialog; back to the launcher once it's closed."""
    TITLE = "Host on Archipelago"

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    def get_default_screen(self) -> Screen:
        return Screen()

    def on_mount(self) -> None:
        self.push_screen(UploadModal(self.path), lambda _: self.exit())


def output_folder() -> str:
    folder = Utils.output_path()
    return folder if os.path.isdir(folder) else Utils.user_path()


def game_picker(title: str) -> Screen:
    """A file picker for generated games, starting in the output folder (also Self-host's)."""
    from aplite.file_picker import FilePicker
    return FilePicker(title, SUFFIXES, "host_dir", output_folder(), start=output_folder())


def picker() -> Screen:
    return game_picker("Host on Archipelago: pick a generated game")


def run(*args: str) -> None:
    """A component func: the launcher passes the picked file; direct launch asks for one."""
    path = args[0] if args else None
    if not path:
        from aplite import prompts
        path = prompts.open_filename("Host on Archipelago: pick a generated game", [("Generated games", SUFFIXES)],
                                     output_folder())
    if path:
        HostApp(Path(path)).run()


def component():
    """Launcher entry for this tool (added to the component list by aplite)."""
    from worlds.LauncherComponents import Component, Type
    return Component("Host on Archipelago", func=run, component_type=Type.TOOL,
                     description="Upload a generated game to the Archipelago website and open its page to create "
                                 "a room.")
