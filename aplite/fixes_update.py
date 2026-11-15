"""
Client fixes between releases: aplite/fixes/fixes.py is downloaded from the main branch only (1) when the user opens
"Manage Client Fixes" in the launcher and checks, or (2) after a client crash, once per game per run, to say whether a
fix for that game exists. Nothing is installed without the user. An installed copy lives in the user folder and is used while
its LOG is longer than the bundled one's (aplite/client_fixes.py, active_path). Source checkouts update with git.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import types
import typing

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Static

from aplite import client_fixes
from aplite.nav import AppliteApp, ButtonRow

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
    current = client_fixes.read_entries(local).get(game)
    new_fix = (entry is not None and client_fixes.applies(entry.versions, game)
               and (current is None or current.code != entry.code))
    package = next((p for p, g in client_fixes.world_packages().items() if g == game), None)
    local_patches = {patch.code for patch in client_fixes.read_patches(local)}
    new_patch = any(patch.why.split("/")[0] == package and patch.code not in local_patches
                    and client_fixes.applies(patch.versions, game) for patch in client_fixes.read_patches(remote))
    if not new_fix and not new_patch:
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


TITLE = "Manage Client Fixes"
"""The launcher entry's name and the screen's heading."""
DESCRIPTION = ("Many apworlds don't play nicely outside the official launcher environment. We maintain a collection of "
               "targeted fixes to apworlds to make them function in aplite.")


def log_table(lines: typing.Iterable[str]) -> Table:
    """LOG lines ("2026-11-15 Banjo-Tooie: what changed") as Date / Game / Change columns; a line in another shape
    goes whole into Change."""
    table = Table(box=None, padding=(0, 2, 0, 0), header_style="bold", expand=True)
    table.add_column("Date", no_wrap=True)
    table.add_column("Game", no_wrap=True, style="bold")
    table.add_column("Change", ratio=1)
    for line in lines:
        date, _, rest = line.partition(" ")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            date, rest = "", line
        game, sep, change = rest.partition(": ")
        table.add_row(*((date, game, change) if sep else (date, "", rest)))
    return table


def heading(text: str, first: bool = False) -> Text:
    return Text(("" if first else "\n") + text, "bold underline")


class ClientFixesScreen(Screen[None]):
    """A screen, so it opens on top of the launcher (no reload just to check). Installing or removing an update
    restarts the launcher when this closes: clients run in the launcher's process, which loaded the old fixes."""
    CSS = """
    ClientFixesScreen #intro { height: auto; padding: 1 1 0 1; }
    ClientFixesScreen VerticalScroll { height: 1fr; border-top: solid $panel; padding: 0 1; }
    ClientFixesScreen VerticalScroll:focus { border-top: solid $border; }
    ClientFixesScreen #status { height: 1; padding: 0 1; color: $text-muted; }
    """
    BINDINGS = [Binding("ctrl+r", "check", "Check for updates"), Binding("ctrl+u", "install", "Install update"),
                Binding("ctrl+t", "remove", "Remove update"), Binding("escape", "close", "Back")]

    def __init__(self) -> None:
        super().__init__()
        self.remote: str | None = None
        self.checking = False
        self.changed = False
        """An update was installed or removed: the launcher restarts on close."""

    def compose(self) -> ComposeResult:
        yield Static(Text.assemble((TITLE, "bold"), "\n", (DESCRIPTION, "dim")), id="intro")
        with ButtonRow(id="buttons"):  # the same actions as the footer keys
            yield Button("Check for updates", id="check", variant="primary")
            yield Button("Install update", id="install")
            yield Button("Remove update", id="remove")
            yield Button("Back", id="close")
        with VerticalScroll():
            yield Static(id="details")
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.show_local()

    def installed(self) -> bool:
        return client_fixes.active_path() != client_fixes.BUNDLED

    def status(self, message: str = "") -> None:
        source = "a downloaded update" if self.installed() else "the fixes included with this version"
        default = f"Using {source}." + ("" if packaged() else " Running from source: git pull gets new fixes.")
        self.query_one("#status", Static).update(message or default)
        self.refresh_bindings()
        for button in self.query("#buttons Button").results(Button):
            allowed = self.check_action(button.id, ())
            button.display = allowed is not False
            button.disabled = allowed is None

    @on(Button.Pressed, "#buttons Button")
    async def pressed(self, event: Button.Pressed) -> None:
        event.stop()
        await self.run_action(event.button.id)
    def show_local(self, message: str = "") -> None:
        source = client_fixes.read_text(client_fixes.active_path())
        packages = client_fixes.world_packages()
        details = Text()
        for game, entry in sorted(client_fixes.read_entries(source).items()):
            versions = f" (made for {', '.join(entry.versions)})" if entry.versions else ""
            details.append(f"{game}{versions}", "bold")
            details.append(f": {entry.why}\n")
        details.append("\n")
        details.append("Source patches\n", "bold underline")
        for patch in client_fixes.read_patches(source):
            details.append(packages.get(patch.why.split("/")[0], patch.why.split("/")[0]), "bold")
            details.append(f" ({patch.why})\n")
        self.query_one("#details", Static).update(Group(
            heading("Log", first=True), log_table(reversed(client_fixes.read_log(source))), heading("Fixes"),
            details))
        self.status(message)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "install":
            return self.remote is not None and packaged()
        if action == "remove":
            return packaged() and self.installed()
        if action == "check":
            return None if self.checking else True  # shown, greyed out while checking
        if action == "close":
            return self.app.screen_stack[0] is not self  # on top of the launcher, not alone
        return True

    def action_check(self) -> None:
        if self.checking:
            return
        self.checking = True
        self.status("Checking for updates…")
        self.download()

    @work(thread=True, exclusive=True)
    def download(self) -> None:
        try:
            source, error = fetch(), None
            client_fixes.read_log(source)
        except Exception as e:
            source, error = None, str(e)
        self.app.call_from_thread(self.downloaded, source, error)

    def downloaded(self, source: str | None, error: str | None) -> None:
        self.checking = False
        if source is None:
            self.status(f"Couldn't check for updates: {error}")
            return
        local = local_source()
        if not newer(source, local):
            self.status("Up to date.")
            return
        self.remote = source
        known = set(client_fixes.read_log(local))
        new_lines = [line for line in client_fixes.read_log(source) if line not in known]
        self.query_one("#details", Static).update(Group(heading("New in the update", first=True),
                                                        log_table(reversed(new_lines))))
        self.status("An update is available: Install update (Ctrl+U) installs it." if packaged() else
                    "An update is available: update your checkout (git pull) to get it.")

    def action_install(self) -> None:
        if self.remote is None:
            return
        try:
            install(self.remote)
        except (OSError, ValueError) as e:
            self.remote = None
            self.show_local(f"Not installed: {e}")
            return
        self.remote = None
        self.changed = True
        self.show_local("Installed." + (" The launcher restarts when you close this, to use it."
                                        if self.check_action("close", ()) else " Restart any open clients to use it."))

    def action_remove(self) -> None:
        if uninstall():
            self.changed = True
            self.show_local("Removed the downloaded update." + (" The launcher restarts when you close this."
                                                                if self.check_action("close", ()) else ""))

    def action_close(self) -> None:
        if self.changed:
            from aplite import nav
            nav.next_step = "launcher"  # __main__ starts a fresh launcher, which loads the new fixes
            self.app.exit()
        else:
            self.dismiss()


class ClientFixesApp(AppliteApp):
    TITLE = TITLE

    def get_default_screen(self) -> Screen:
        return ClientFixesScreen()


def component():
    """Launcher entry for this tool (added to the component list by aplite)."""
    from worlds.LauncherComponents import Component, Type
    return Component(TITLE, func=run, component_type=Type.TOOL,
                     description="Fixes that make apworlds' clients work in aplite: check for updates and install "
                                 "them.")


def run(*args: str) -> None:  # a component func: the launcher may pass args, which it doesn't use
    ClientFixesApp().run()
