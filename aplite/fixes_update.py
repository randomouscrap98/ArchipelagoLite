"""
Client fixes between releases: aplite/fixes/fixes.py is downloaded from the main branch only (1) when the user opens
"Client fixes" in the launcher and checks, or (2) after a client crash, once per game per run, to say whether a fix for
that game exists. Nothing is installed without the user. An installed copy lives in the user folder and is used while
its LOG is longer than the bundled one's (aplite/client_fixes.py, active_path). Source checkouts update with git.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import types

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Footer, Static

from aplite import client_fixes
from aplite.nav import AppliteApp

URL = "https://raw.githubusercontent.com/randomouscrap98/ArchipelagoLite/main/aplite/fixes/fixes.py"
TIMEOUT = 10
"""Seconds for the download (a crash notice waits this long at most)."""


def packaged() -> bool:
    """A release (tar.gz, AppImage, zip), as opposed to a source checkout, which gets fixes with git."""
    from aplite import version
    return os.path.isfile(version.VERSION_FILE)


def fetch() -> str:
    import ssl
    import urllib.request

    import certifi
    context = ssl.create_default_context(cafile=certifi.where())
    request = urllib.request.Request(URL, headers={"User-Agent": "ArchipelagoLite"})
    with urllib.request.urlopen(request, timeout=TIMEOUT, context=context) as response:
        return response.read().decode("utf-8")


def local_source() -> str:
    return client_fixes.read_text(client_fixes.active_path())


def newer(remote: str, local: str) -> bool:
    return len(client_fixes.read_log(remote)) > len(client_fixes.read_log(local))


def notice_for(game: str) -> str | None:
    """Downloads the fixes file: a message if it has a fix for this game (and its installed version) that's not in use
    here, else None. Blocking; raises on network errors."""
    remote, local = fetch(), local_source()
    if not newer(remote, local):
        return None
    entry = client_fixes.read_entries(remote).get(game)
    if entry is None or not client_fixes.applies(entry.versions, game):
        return None
    current = client_fixes.read_entries(local).get(game)
    if current is not None and current.code == entry.code:
        return None
    if packaged():
        return (f'A client fix for {game} is available. Open "Client fixes" in the launcher to install it, then '
                f"restart this client.")
    return f"A client fix for {game} is available: update your ArchipelagoLite checkout (git pull) to get it."


_checked: set[str] = set()
"""Games already checked this run."""


def on_error(record: logging.LogRecord) -> None:
    """A client logged an exception (aplite/tui.py LogBuffer): if a world's code is in the traceback, check for a fix
    for that game in the background and log a notice if there is one."""
    tb = record.exc_info[2] if record.exc_info else None
    if tb is None:
        return
    try:
        loop = asyncio.get_running_loop()
        game = client_fixes.game_for_traceback(tb)
    except Exception:
        return
    if not game or game in _checked:
        return
    _checked.add(game)
    task = loop.create_task(_notify(game), name="aplite client fix check")
    _tasks.add(task)  # the loop holds tasks only weakly
    task.add_done_callback(_tasks.discard)


_tasks: set[asyncio.Task] = set()


async def _notify(game: str) -> None:
    try:
        message = await asyncio.to_thread(notice_for, game)
    except Exception as e:
        logging.getLogger("aplite").debug(f"client fix check for {game} failed: {e}")
        return
    if message:
        logging.getLogger("Client").info(message)


def crash_notice(tb: types.TracebackType | None, game: str | None = None) -> None:
    """A component crashed outside its TUI (aplite/__main__.py report_crash): print a notice if there's a fix."""
    game = client_fixes.game_for_traceback(tb) or game
    if not game or game in _checked:
        return
    _checked.add(game)
    try:
        message = notice_for(game)
    except Exception:
        return
    if message:
        print(f"\n{message}")


def install(source: str) -> None:
    """Install a downloaded fixes file. It's loaded first (a file that doesn't load in this aplite isn't installed),
    then replaces any previous download. Raises ValueError with the reason."""
    if not newer(source, local_source()):
        raise ValueError("this aplite already has these fixes")
    target = client_fixes.installed_path()
    fd, temp = tempfile.mkstemp(prefix=".aplite_client_fixes-", suffix=".py", dir=os.path.dirname(target))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(source)
        try:
            client_fixes.load_file(temp)
        except Exception as e:
            raise ValueError(f"it doesn't load in this version of aplite ({e}); "
                             f"update aplite to get these fixes") from e
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.remove(temp)


def uninstall() -> bool:
    try:
        os.remove(client_fixes.installed_path())
        return True
    except FileNotFoundError:
        return False


class ClientFixesApp(AppliteApp):
    TITLE = "Client fixes"
    CSS = """
    #status { padding: 1 2 0 2; height: auto; }
    #buttons { height: auto; padding: 1 2; }
    #buttons Button { margin-right: 2; }
    VerticalScroll { height: 1fr; border-top: solid $panel; padding: 0 2; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.remote: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Horizontal(id="buttons"):
            yield Button("Check for updates", id="check", compact=True, variant="primary")
            yield Button("Install update", id="install", compact=True, disabled=True)
            yield Button("Remove update", id="remove", compact=True)
        with VerticalScroll():
            yield Static(id="details")
        yield Footer()

    def on_mount(self) -> None:
        self.show_local()

    def show_local(self, message: str = "") -> None:
        path = client_fixes.active_path()
        source = client_fixes.read_text(path)
        log = client_fixes.read_log(source)
        installed = path != client_fixes.BUNDLED
        status = Text.assemble(
            ("Fixes for game clients that break in aplite. ", "bold"),
            "They can be updated between releases; a fix only affects its own game's client.\n",
            f"In use: {'downloaded update' if installed else 'the fixes included with this version'} "
            f"({len(log)} log entr{'y' if len(log) == 1 else 'ies'}).")
        if not packaged():
            status.append("\nRunning from source: update your checkout (git pull) to get new fixes.", "dim")
        if message:
            status.append(f"\n{message}", "bold")
        self.query_one("#status", Static).update(status)
        self.query_one("#remove", Button).display = installed and packaged()
        self.query_one("#install", Button).display = packaged()
        details = Text()
        details.append("Fixes\n", "bold underline")
        for game, entry in sorted(client_fixes.read_entries(source).items()):
            versions = f" (version {', '.join(entry.versions)})" if entry.versions else ""
            details.append(f"{game}{versions}", "bold")
            details.append(f": {entry.why}\n")
        details.append("\nLog\n", "bold underline")
        for line in reversed(log):
            details.append(f"{line}\n")
        self.query_one("#details", Static).update(details)

    @on(Button.Pressed, "#check")
    def check(self) -> None:
        self.query_one("#check", Button).disabled = True
        self.query_one("#status", Static).update("Checking…")
        self.download()

    @work(thread=True, exclusive=True)
    def download(self) -> None:
        try:
            source, error = fetch(), None
            client_fixes.read_log(source)
        except Exception as e:
            source, error = None, str(e)
        self.call_from_thread(self.downloaded, source, error)

    def downloaded(self, source: str | None, error: str | None) -> None:
        self.query_one("#check", Button).disabled = False
        if source is None:
            self.show_local(f"Couldn't check for updates: {error}")
            return
        local = local_source()
        if not newer(source, local):
            self.show_local("Up to date.")
            return
        self.remote = source
        known = set(client_fixes.read_log(local))
        new_lines = [line for line in client_fixes.read_log(source) if line not in known]
        self.show_local("An update is available" + (": install it, then restart your clients." if packaged() else
                                                    ": update your checkout (git pull) to get it."))
        self.query_one("#install", Button).disabled = not packaged()
        details = Text()
        details.append("New in the update\n", "bold underline")
        for line in reversed(new_lines):
            details.append(f"{line}\n")
        self.query_one("#details", Static).update(details)

    @on(Button.Pressed, "#install")
    def install_update(self) -> None:
        if self.remote is None:
            return
        try:
            install(self.remote)
        except (OSError, ValueError) as e:
            self.show_local(f"Not installed: {e}")
        else:
            self.show_local("Installed. Restart any open clients to use it.")
        self.remote = None
        self.query_one("#install", Button).disabled = True

    @on(Button.Pressed, "#remove")
    def remove_update(self) -> None:
        removed = uninstall()
        self.show_local("Removed the downloaded update; using the fixes included with this version." if removed else
                        "")


def component():
    """Launcher entry for this tool (added to the component list by aplite)."""
    from worlds.LauncherComponents import Component, Type
    return Component("Client fixes", func=run, component_type=Type.TOOL,
                     description="Fixes for game clients that break in aplite, updated between releases: check for "
                                 "and install them.")


def run(*args: str) -> None:  # a component func: the launcher may pass args, which it doesn't use
    ClientFixesApp().run()
