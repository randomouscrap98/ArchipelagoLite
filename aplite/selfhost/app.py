"""
Self-host TUI: start/stop the self-host server (it keeps running after leaving; coming back attaches to it), add
games, a tab per active game (state, owner link, server console: log tail + commands), the Games box (activate,
deactivate, delete) and settings. See aplite/selfhost/__init__.py.
"""
from __future__ import annotations

import datetime
import os
import typing
from pathlib import Path

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Input, Label, OptionList, RichLog, Static, Tab, Tabs
from textual.widgets.option_list import Option as ListOption

import Utils
from aplite import colors, glyphs, nav, selfhost
from aplite.master_detail import MasterDetailScreen, Rows, TextScroll, detail_pane, heading
from aplite.nav import AppliteApp

LOG_START = 64 * 1024  # bytes of an existing log shown when attaching


def ago(when: datetime.datetime) -> str:
    seconds = int((Utils.utcnow() - when).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min ago"


class ServerSettingsScreen(MasterDetailScreen, ModalScreen):
    """selfhost/config.yaml in the editors' layout (rows + editor + description), in a box over the dimmed Self-host
    screen: Esc on the rows (or the footer's Back) closes it."""
    CSS = MasterDetailScreen.CSS + """
    ServerSettingsScreen { align: center middle; background: $background 60%; }  /* dims Self-host, still visible */
    #settings-box { width: 90%; height: auto; max-height: 90%; border: thick $primary; background: $surface;
                    border-title-style: bold; }
    #settings-box > Horizontal { height: auto; }  /* fixed to the rows' height in on_mount: long help scrolls */
    """
    BINDINGS = [Binding("escape", "back_to_rows", "Back"),  # shown (clickable): editor -> rows -> Self-host
                Binding("ctrl+s", "save", "Save"), Binding("ctrl+r", "reset", "Reset setting"),
                Binding("ctrl+o", "browse", "Browse")]
    BROWSE_PREF = "selfhost_browse_dir"
    STRIP_TEXT = True
    APPLY_NOTE = "Changes apply when the server (re)starts."

    def __init__(self, saved: typing.Callable[[dict[str, typing.Any]], None]) -> None:
        super().__init__(closable=True)
        self.saved = saved
        self.config = selfhost.load_config()
        self.values = {key: self.config[key] for key in selfhost.editable_settings()}
        self.name_width = max(map(len, self.values))

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-box"):
            with Horizontal():
                yield Rows(id="rows")
                yield detail_pane()
        yield Footer()

    def on_mount(self) -> None:
        box = self.query_one("#settings-box")
        box.query_one(Horizontal).styles.height = len(self.values) + 2  # the rows and their border
        box.border_title = "Server settings"
        box.border_subtitle = selfhost.root("config.yaml")
        self.show_rows([ListOption(self.row(key), id=key) for key in self.values])
        self.query_one("#rows", Rows).focus()

    # rows

    def row_keys(self) -> typing.Iterable[str]:
        return self.values

    def row(self, key: str) -> Text:
        value = self.values[key]
        return self.fitted_row(key, str(value) if value != "" else "(not set)", value != self.default_of(key))

    def stepped(self, key: str, delta: int) -> typing.Any:
        value = self.values[key]
        return max(0, value + delta) if isinstance(value, int) else None

    def update_editor(self, key: str) -> bool:
        if not self.query("#int"):
            return False
        self.query_one("#int", Input).value = str(self.values[key])
        return True

    def default_of(self, key: str) -> typing.Any:
        return selfhost.SETTINGS[key].default

    def set_value(self, key: str, value: typing.Any) -> None:
        if self.values[key] != value:
            self.values[key] = value
            self.dirty = True
        self.refresh_row(key)

    # editor

    async def build_editor(self) -> None:
        key = self.editor_key
        setting = selfhost.SETTINGS[key]
        value, default = self.values[key], setting.default
        widgets: list = heading(key, f"default: {default if default != '' else '(not set)'}")
        if setting.path:
            widgets += self.path_editor(str(value))
        elif isinstance(default, int):
            widgets.append(Input(str(value), type="integer", id="int", placeholder="Number (Enter to set)"))
        else:
            widgets.append(Input(str(value), id="str", placeholder="Text (Enter to set)"))
        widgets.append(Label("", id="error"))
        await self.mount_editor(widgets)
        self.update_exists()
        self.show_doc(setting.description + "\n\n" + self.APPLY_NOTE)

    def resolved_path(self, key: str) -> str:
        return os.path.abspath(os.path.expanduser(self.values[key])) if self.values[key] else ""

    def action_save(self) -> None:
        if not self.dirty:
            self.notify("No changes to save")
            return
        values = self.values
        active = len(selfhost.active())
        if values["room_count"] < max(1, active):
            problem = f"room_count can't be below 1 or the {active} active game(s): deactivate some in Games first."
        elif bool(values["cert"]) != bool(values["key"]):
            problem = "Set both cert and key (or neither)."
        else:
            problem = next((f"{key} file not found: {values[key]}" for key in ("cert", "key")
                            if values[key] and not os.path.isfile(values[key])), None)
        if problem:
            self.notify(problem, severity="error", timeout=8)
            return
        config = {**self.config, **values}
        try:
            selfhost.save_config(config)
        except OSError as e:
            self.notify(f"Could not save: {e}", severity="error", timeout=10)
            return
        self.config, self.dirty = config, False
        self.refresh_rows()
        self.saved(config)


class GamesList(OptionList):
    """A click only highlights a game; Enter (OptionSelected) shows it."""
    async def _on_click(self, event) -> None:  # Textual's private click handler: re-check on Textual bumps
        option = event.style.meta.get("option")
        if option is not None and not self.get_option_at_index(option).disabled:
            self.highlighted = option


def already_loaded(path: str) -> selfhost.Info | None:
    """The newest loaded game from this zip: the same path, or the same name (AP_<seed>: a copy of it)."""
    source, name = os.path.abspath(path), Path(path).stem
    return next((info for info in selfhost.games() if info.get("source") == source or info["name"] == name), None)


class AlreadyLoaded(ModalScreen[bool]):
    """New game picked a game that's already loaded: another copy is a separate room with its own, empty progress."""
    CSS = """
    AlreadyLoaded { align: center middle; }
    #loaded-box { width: 70; max-width: 90%; height: auto; border: thick $warning; background: $surface;
                  padding: 0 1; }
    #loaded-buttons { height: auto; align-horizontal: right; margin-top: 1; }
    #loaded-buttons Button { margin-left: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, info: selfhost.Info) -> None:
        super().__init__()
        self.info = info

    def compose(self) -> ComposeResult:
        with Vertical(id="loaded-box"):
            yield Static(Text.assemble(
                (f"{self.info['name']} is already loaded", "bold"), f" (loaded {self.info['loaded'][:16]}).\n\n",
                "Its room and saved progress are in Games (Ctrl+G). Another copy is a separate room that starts "
                "from nothing, and players could join the wrong one."))
            with Horizontal(id="loaded-buttons"):
                yield Button("Cancel", id="loaded-cancel", variant="primary")
                yield Button("Load another copy", id="loaded-again", variant="warning")

    def on_mount(self) -> None:
        self.query_one("#loaded-cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "loaded-again")

    def action_cancel(self) -> None:
        self.dismiss(False)


class GamesScreen(ModalScreen[typing.Optional[str]]):
    """Every loaded game (newest first) with its details; activate / deactivate / delete them here. Dismisses with
    an active game's room id to show it on the main screen (Show), else None."""
    CSS = """
    GamesScreen { align: center middle; }
    #games-box { width: 90%; height: 80%; border: thick $primary; background: $surface; border-title-style: bold; }
    #games-list { width: 1fr; height: 1fr; text-wrap: nowrap; text-overflow: ellipsis; }
    #games-detail { width: 1fr; height: 1fr; padding: 0 1; border: tall $border-blurred; }  /* like the list */
    #games-detail:focus { border: tall $border; }
    #games-buttons { height: auto; align-horizontal: right; }
    #games-buttons Button { margin-left: 1; }
    """
    BINDINGS = [Binding("enter", "show", "Show"), Binding("space", "toggle", "Activate/deactivate"),
                Binding("delete", "delete", "Delete"), Binding("escape", "close", "Back")]

    def __init__(self, running: bool) -> None:
        super().__init__()
        self.running = running
        self.config = selfhost.load_config()
        self.games: list[selfhost.Info] = []
        self.confirm_delete = nav.Confirm()

    def compose(self) -> ComposeResult:
        with Vertical(id="games-box"):
            with Horizontal():
                yield GamesList(id="games-list")
                with TextScroll(id="games-detail"):
                    yield Static(id="games-text")
            with Horizontal(id="games-buttons"):
                yield Button("Show", id="games-show")
                yield Button("Activate", id="games-toggle", variant="primary")
                yield Button("Delete", id="games-delete", variant="error")
                yield Button("Back", id="games-back")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#games-box").border_title = "Games"
        self.fill()
        self.query_one("#games-list", OptionList).focus()
        # again after the first layout: details shown during mount kept a 1-line height
        self.call_after_refresh(self.show_detail)

    def fill(self, keep: str | None = None) -> None:
        self.games = selfhost.games()
        active = selfhost.active()
        games_list = self.query_one("#games-list", OptionList)
        games_list.clear_options()
        games_list.add_options([ListOption(self.label(info, info["room"] in active), id=info["room"])
                                for info in self.games])
        ids = [info["room"] for info in self.games]
        if ids:
            games_list.highlighted = ids.index(keep) if keep in ids else 0
        self.query_one("#games-box").border_subtitle = (f"{len(active)} of {self.config['room_count']} room(s) "
                                                        "active")
        self.show_detail()

    @staticmethod
    def label(info: selfhost.Info, active: bool) -> Text:
        return Text.assemble((f"{glyphs.symbol('on' if active else 'off')} ", "green" if active else "dim"),
                             (info["name"], "bold"),
                             f"  {len(info['players'])} players  {info['loaded'][:16]}")

    def selected(self) -> selfhost.Info | None:
        option = self.query_one("#games-list", OptionList).highlighted_option
        return next((info for info in self.games if option and info["room"] == option.id), None)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.confirm_delete.reset()
        self.show_detail()

    def show_detail(self) -> None:
        info = self.selected()
        toggle, delete = self.query_one("#games-toggle", Button), self.query_one("#games-delete", Button)
        toggle.disabled = delete.disabled = info is None
        if info is None:
            self.query_one("#games-show", Button).disabled = True
            self.query_one("#games-text", Static).update(Text(
                "No games yet. New game (Ctrl+O) on the main screen adds one.", style="dim"))
            return
        active = info["room"] in selfhost.active()
        toggle.label = "Deactivate" if active else "Activate"
        self.query_one("#games-show", Button).disabled = not active
        text = Text.assemble((info["name"] + "\n", "bold"),
                             ("Active (hosted)\n", "green") if active else ("Inactive (kept, not hosted)\n", "dim"),
                             f"Loaded {info['loaded']}\n")
        state = selfhost.room_state(info)
        if state:
            local = state["last_activity"].replace(tzinfo=datetime.timezone.utc).astimezone()
            text.append(f"Last activity {local:%Y-%m-%d %H:%M:%S} ({ago(state['last_activity'])})\n")
        text.append(f"Source: {info['source']}\n\n{len(info['players'])} players:\n")
        for _, name, game in info["players"]:
            text.append(f"  {name} ({game})\n")
        self.query_one("#games-text", Static).update(text)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.action_show()

    def action_show(self) -> None:
        info = self.selected()
        if info is None:
            return
        if info["room"] not in selfhost.active():
            self.notify(f"{info['name']} isn't active: Activate it first.", severity="warning")
            return
        self.dismiss(info["room"])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        {"games-show": self.action_show, "games-toggle": self.action_toggle, "games-delete": self.action_delete,
         "games-back": self.action_close}[event.button.id]()

    def action_toggle(self) -> None:
        info = self.selected()
        if info is None:
            return
        if info["room"] in selfhost.active():
            selfhost.deactivate(info, self.running)
            self.notify(f"{info['name']} deactivated: no longer hosted, its data is kept.")
        elif error := selfhost.activate(info, self.config):
            self.notify(error, severity="warning", timeout=8)
        else:
            self.notify(f"{info['name']} activated." + ("" if self.running else " Start the server to host it."))
        self.fill(keep=info["room"])

    def action_delete(self) -> None:
        info = self.selected()
        if info is None:
            return
        if info["room"] in selfhost.active():
            self.notify("Deactivate it first.", severity="warning")
            return
        if not self.confirm_delete(self, info["room"], f"Delete {info['name']} and its progress for good? Press Delete "
                                                       f"again to confirm."):
            return
        selfhost.delete_game(info)
        self.notify(f"Deleted {info['name']}.")
        self.fill()

    def action_close(self) -> None:
        self.dismiss(None)


SCREEN_CSS = """
.heading { padding: 0 1; text-style: bold; }
#server, #room { padding: 0 1; height: auto; }
#buttons { height: auto; }
#buttons Button { margin-right: 1; }
#room-bar { height: 1; padding: 0 1; }
#room-bar Button { margin-right: 2; }
#room-loaded { padding-left: 1; }
#console { height: 1fr; border: tall $border-blurred; }
#console:focus { border: tall $border; }
"""


class SelfHostScreen(Screen):
    CSS = SCREEN_CSS
    BINDINGS = [Binding("ctrl+r", "toggle_server", "Start/stop server"), Binding("ctrl+o", "new_game", "New game"),
                Binding("ctrl+g", "games", "Games"),
                Binding("ctrl+e", "settings", "Settings"), Binding("ctrl+t", "open_room", "Open room", show=False),
                Binding("ctrl+n", "switch_room(1)", "Next room"), Binding("ctrl+b", "switch_room(-1)", "Prev room"),
                Binding("ctrl+w", "wake", "Wake room", show=False), Binding("escape", "back", "Back", show=False)]

    def __init__(self, load: str | None = None) -> None:
        super().__init__()
        self.initial_load = load
        self.busy = ""  # what's running in the background (start/stop/load), shown instead of the server state
        self.status: dict[str, typing.Any] | None = None
        """The server process's status file (it exists: starting or serving); None when stopped."""
        self.serving = False
        """The server answers (status "serving"): rooms can be opened and woken."""
        self.active: list[str] = []  # active room ids, as the tabs show them
        self.selected: str | None = None  # room id of the shown game
        self.syncing_tabs = False
        self.info: selfhost.Info | None = None  # the shown game
        self.log_file: str | None = None
        self.log_offset = 0
        self.focus_before: typing.Any = None

    def compose(self) -> ComposeResult:
        yield Label("Server", classes="heading")
        yield Static(id="server")
        with Horizontal(id="buttons"):
            yield Button("Start server", id="toggle", variant="primary")
            yield Button("New game", id="new")
            yield Button("Games", id="games")
            yield Button("Settings", id="settings")
        yield Label("Room", id="room-heading", classes="heading")
        yield Tabs(id="rooms")
        with Horizontal(id="room-bar"):  # the selected game's own actions
            yield Button("Open room ^T", id="open", compact=True)
            yield Button("Wake room ^W", id="wake", compact=True)
            yield Label(id="room-loaded")
        yield Static(id="room")
        yield Label("Server console", classes="heading")
        yield RichLog(id="console", wrap=True, markup=False)
        yield Input(placeholder="Server command, e.g. /players or /help (Enter sends; wakes a sleeping room)",
                    id="command")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_state()
        self.set_interval(1, self.refresh_state)
        self.query_one("#toggle", Button).focus()
        if self.initial_load:
            self.new_game(self.initial_load)

    # --- state ---

    def refresh_state(self) -> None:
        self.status = selfhost.server_status()
        config = selfhost.load_config()
        self.serving = bool(self.status and self.status.get("serving"))
        self.refresh_tabs()
        self.info = selfhost.game(self.selected) if self.selected else None
        server = self.query_one("#server", Static)
        toggle = self.query_one("#toggle", Button)
        toggle.label = "Stop server" if self.status else "Start server"
        for button in self.query("#buttons Button"):
            button.disabled = bool(self.busy)
        if self.busy:
            server.update(Text(self.busy, style="yellow"))
        elif self.serving:
            url = selfhost.base_url(config, self.status)
            server.update(Text.assemble(
                (f"{glyphs.symbol('on')} Running ", colors.style("connected") + Style(bold=True)), (url, f"underline link {url}"),
                f"  (listening on {self.status['bind']}:{self.status['port']})\n",
                ("Keeps running after you leave; stop it here.", "dim")))
        elif self.status:
            server.update(Text("Starting…", style="yellow"))
        else:
            server.update(Text.assemble((f"{glyphs.symbol('off')} Stopped", "bold"), ("  Ctrl+R starts it.", "dim")))
        self.query_one("#room", Static).update(self.room_text(config, self.serving))
        self.query_one("#room-bar").display = self.info is not None
        if self.info:
            self.query_one("#room-loaded", Label).update(Text(f"loaded {self.info['loaded']}", style="dim"))
        # without tabs (one game) the heading names it
        self.query_one("#room-heading", Label).update(f"Room: {self.info['name']}" if self.info else "Room")
        self.query_one("#open", Button).disabled = bool(self.busy) or not (self.serving and self.info)
        self.query_one("#wake", Button).disabled = bool(self.busy) or not (self.serving and self.info)
        self.tail_log()

    def refresh_tabs(self) -> None:
        """One tab per active game, shown when there's more than one; keeps the selection if it's still active."""
        active = selfhost.active()
        if self.selected not in active:
            self.selected = active[0] if active else None
        tabs = self.query_one("#rooms", Tabs)
        tabs.display = len(active) > 1
        self.query_one("#room-heading").display = len(active) <= 1
        if active != self.active and not self.syncing_tabs:
            self.syncing_tabs = True
            self.run_worker(self.sync_tabs(active))

    async def sync_tabs(self, active: list[str]) -> None:
        """Rebuilds the tabs (clear/add are async: adding right after clear hit duplicate ids). Adding activates
        tabs on its own, so the selection is put back afterwards (those events come first, in order)."""
        want = self.selected
        tabs = self.query_one("#rooms", Tabs)
        try:
            await tabs.clear()
            for number, room in enumerate(active, 1):  # numbered: the same seed can be loaded twice
                info = selfhost.game(room)
                await tabs.add_tab(Tab(f"{number} {info['name'] if info else room}", id=tab_id(room)))
            self.active = active
            self.refresh_bindings()  # Next/Prev room show with two or more
            if want in active:
                self.selected = want
                tabs.active = tab_id(want)
        finally:
            self.syncing_tabs = False

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        if event.tab is not None:
            room = next((room for room in self.active if tab_id(room) == event.tab.id), None)
            if room and room != self.selected:
                self.selected = room
                self.refresh_state()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        return len(self.active) > 1 if action == "switch_room" else True  # the tabs show only then

    def action_switch_room(self, delta: int) -> None:
        """Ctrl+N / Ctrl+B: next / previous room tab, like the client's tabs."""
        if self.selected in self.active:
            self.query_one("#rooms", Tabs).active = tab_id(
                self.active[(self.active.index(self.selected) + delta) % len(self.active)])

    def select(self, room: str) -> None:
        self.selected = room
        self.refresh_state()
        if self.query_one("#rooms", Tabs).display:
            self.query_one("#rooms", Tabs).active = tab_id(room)

    def room_text(self, config: dict[str, typing.Any], running: bool) -> Text:
        if not self.info:
            return Text("No game active. Ctrl+O adds a generated game (.zip / .archipelago); Games (Ctrl+G) "
                        "activates one loaded before.", style="dim")
        info = self.info
        players = ", ".join(f"{name} ({game})" for _, name, game in info["players"])
        text = Text(f"{len(info['players'])} players: {players}\n")
        state = selfhost.room_state(info)
        address = selfhost.public_address(config, self.status)
        if not running:
            text.append("Game server: not running (the website server is stopped)\n", style="dim")
        elif state is None:
            text.append("Game server: unknown (can't read the room database)\n", style="red")
        elif state["last_port"] == -1:
            text.append("Game server: failed to start, see the console. Wake retries.\n", style="red")
        elif state["awake"] and not state["last_port"]:
            text.append("Game server: starting…\n", style="yellow")
        elif state["awake"]:
            port = state["last_port"]
            text.append_text(Text.assemble("Game server: ", (f"{address}:{port}", "bold"),
                                           f"  awake, last activity {ago(state['last_activity'])}\n"))
        else:
            text.append(f"Game server: sleeping (no activity for {state['timeout'] // 60} min). "
                        "Opening the room page or Ctrl+W wakes it.\n", style="yellow")
        base = selfhost.base_url(config, self.status)
        url = selfhost.owner_link(info, base)  # the other pages are linked from the room page it opens
        text.append_text(Text.assemble("Owner link (room page with its console; keep it to yourself):\n",
                                       (url, f"link {url}"), "\n"))
        text.rstrip()
        return text

    def tail_log(self) -> None:
        console = self.query_one("#console", RichLog)
        path = selfhost.log_path(self.info) if self.info else None
        if path != self.log_file:
            console.clear()
            self.log_file, self.log_offset = path, 0
            if path and os.path.exists(path):
                self.log_offset = max(0, os.path.getsize(path) - LOG_START)
        if not path or not os.path.exists(path):
            return
        if os.path.getsize(path) < self.log_offset:
            self.log_offset = 0  # replaced
        with open(path, "rb") as f:
            f.seek(self.log_offset)
            data = f.read()
        end = data.rfind(b"\n") + 1  # whole lines only
        if not end:
            return
        self.log_offset += end
        for line in data[:end].decode("utf-8-sig", errors="replace").splitlines():
            console.write(line)

    # --- actions ---

    def in_background(self, busy: str, work: typing.Callable[[], str | None]) -> None:
        """Runs `work` in a thread (one at a time); its returned string is shown as a notification."""
        if self.busy:
            return
        self.busy = busy
        self.focus_before = self.focused or self.query_one("#toggle", Button)
        self.refresh_state()

        def run() -> None:
            try:
                message, error = work(), False
            except Exception as e:
                message, error = str(e), True
            self.app.call_from_thread(self.done, message, error)
        self.run_worker(run, thread=True)

    def done(self, message: str | None, error: bool) -> None:
        self.busy = ""
        self.refresh_state()
        if isinstance(self.focus_before, Button):  # disabling the buttons moved focus away
            self.focus_before.focus()
        if message:
            self.notify(message, severity="error" if error else "information", timeout=8)

    def action_toggle_server(self) -> None:
        if self.status:
            self.in_background("Stopping…", lambda: None if selfhost.stop_server() else "The server didn't stop.")
        else:
            self.in_background("Starting (loading worlds)…", start_and_wait)

    def action_new_game(self) -> None:
        if self.busy:
            return
        from aplite.webhost import game_picker
        self.app.push_screen(game_picker("Self-host: pick a generated game"),
                             lambda path: path and self.picked_game(str(path)))

    def picked_game(self, path: str) -> None:
        if existing := already_loaded(path):
            self.app.push_screen(AlreadyLoaded(existing), lambda again: again and self.new_game(path))
        else:
            self.new_game(path)

    def new_game(self, path: str) -> None:
        """Loads a game (kept in the DB from now on) and activates it if a room is free."""
        def work() -> str:
            info = selfhost.load_game(path)
            error = selfhost.activate(info, selfhost.load_config())
            if error:
                return f"Added {info['name']}, but not active. {error} Then activate it in Games (Ctrl+G)."
            self.app.call_from_thread(self.select, info["room"])
            if selfhost.server_status():
                return f"Added {info['name']}."
            self.app.call_from_thread(setattr, self, "busy", "Starting the server (loading worlds)…")
            return " ".join(filter(None, [f"Added {info['name']}.", start_and_wait()]))
        self.in_background(f"Loading {Path(path).name}…", work)

    def action_games(self) -> None:
        if not self.busy:
            self.app.push_screen(GamesScreen(running=bool(self.status)),
                                 lambda room: self.select(room) if room else self.refresh_state())

    def action_settings(self) -> None:
        def saved(config: dict[str, typing.Any]) -> None:
            selfhost.set_timeouts(int(config["room_timeout"]))
            self.notify("Saved." + (" Restart the server (stop, then start) to apply." if self.status else ""),
                        timeout=8)
        self.app.push_screen(ServerSettingsScreen(saved))

    def action_open_room(self) -> None:
        if not (self.info and self.serving):
            return
        from aplite.desktop import open_link
        # the owner link: the host's browser gets the room page with its console
        url = selfhost.owner_link(self.info, selfhost.base_url(selfhost.load_config(), self.status))
        open_link(self, url)

    def action_wake(self) -> None:
        if self.info and self.serving:
            selfhost.wake(self.info)
            self.refresh_state()

    def action_back(self) -> None:
        """Pushed from Generate GUI: back there (the server keeps running)."""
        if len(self.app.screen_stack) > 1:
            self.app.pop_screen()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        {"toggle": self.action_toggle_server, "new": self.action_new_game, "games": self.action_games,
         "settings": self.action_settings, "open": self.action_open_room, "wake": self.action_wake}[event.button.id]()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        text = event.value.strip()
        if not text:
            return
        if not self.info:
            self.notify("No game active.", severity="warning")
            return
        state = selfhost.room_state(self.info)
        if state and not state["awake"]:
            selfhost.wake(self.info)
        selfhost.send_command(self.info, text)
        self.query_one("#console", RichLog).write(Text(f"> {text}", style="bold"))
        event.input.value = ""


def tab_id(room: str) -> str:
    return "room-" + room.replace("-", "")


def start_and_wait() -> str | None:
    """For in_background: starts the server, raises if it doesn't come up."""
    selfhost.start_server()
    if selfhost.wait_for_server():
        return None
    raise RuntimeError(f"The server didn't start; see {selfhost.root('server.log')}")


class SelfHostApp(AppliteApp):
    TITLE = "Self-host"
    CSS = SCREEN_CSS  # a default screen's own CSS isn't loaded

    def __init__(self, load: str | None = None) -> None:
        super().__init__()
        self.initial_load = load

    def get_default_screen(self) -> Screen:
        return SelfHostScreen(self.initial_load)


def run(*args: str) -> None:
    SelfHostApp(args[0] if args else None).run()


def component():
    """Launcher entry for this tool (added to the component list by aplite)."""
    from worlds.LauncherComponents import Component, Type
    return Component("Self-host", func=run, component_type=Type.TOOL,
                     description="Host a generated game on this machine with the website's room page, trackers and "
                                 "downloads. The server keeps running in the background.")
