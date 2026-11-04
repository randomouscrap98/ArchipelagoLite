"""
Self-host TUI: start/stop the self-host server (it keeps running after leaving; coming back attaches to it), load a
generated game (the previous room is archived), the room's links and state, and its server console (log tail +
commands). See aplite/selfhost/__init__.py.
"""
from __future__ import annotations

import datetime
import os
import threading
import typing
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Input, Label, RichLog, Static
from textual.widgets.option_list import Option as ListOption

import Utils
from aplite import glyphs, selfhost
from aplite.file_picker import FilePicker, PathInput
from aplite.master_detail import MasterDetailScreen, Rows, editor_event
from aplite.nav import AppliteApp

SUFFIXES = (".zip", ".archipelago")
LOG_START = 64 * 1024  # bytes of an existing log shown when attaching


def ago(when: datetime.datetime) -> str:
    seconds = int((Utils.utcnow() - when).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min ago"


PATH_KEYS = {"cert": False, "key": False, "archive_dir": True}  # path settings: folder?


class SettingsScreen(MasterDetailScreen, ModalScreen):
    """selfhost/config.yaml in the editors' layout (rows + editor + description), in a box over the dimmed Self-host
    screen: Esc on the rows (or the footer's Back) closes it."""
    CSS = MasterDetailScreen.CSS + """
    SettingsScreen { align: center middle; background: $background 60%; }  /* dims Self-host, still visible */
    #settings-box { width: 90%; height: auto; max-height: 90%; border: thick $primary; background: $surface;
                    border-title-style: bold; }
    #settings-box > Horizontal { height: auto; }
    #settings-box #detail { height: auto; }
    #settings-box #doc { height: auto; }
    """
    BINDINGS = [Binding("escape", "back_to_rows", "Back"),  # shown (clickable): editor -> rows -> Self-host
                Binding("ctrl+s", "save", "Save"), Binding("ctrl+r", "reset", "Reset setting"),
                Binding("ctrl+b", "browse", "Browse", show=False)]

    def __init__(self, saved: typing.Callable[[dict[str, typing.Any]], None]) -> None:
        super().__init__(closable=True)
        self.saved = saved
        self.config = selfhost.load_config()
        self.values = {key: self.config[key] for key in selfhost.DESCRIPTIONS}
        self.dirty = False
        self.name_width = max(map(len, selfhost.DESCRIPTIONS))

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-box"):
            with Horizontal():
                yield Rows(id="rows")
                with Vertical(id="detail"):
                    yield Vertical(id="editor")
                    with VerticalScroll(id="doc"):
                        yield Static(id="doc-text")
        yield Footer()

    def on_mount(self) -> None:
        box = self.query_one("#settings-box")
        box.border_title = "Server settings"
        box.border_subtitle = selfhost.root("config.yaml")
        self.show_rows([ListOption(self.row(key), id=key) for key in self.values])
        self.query_one("#rows", Rows).focus()

    # rows

    def row_keys(self) -> typing.Iterable[str]:
        return self.values

    def row(self, key: str) -> Text:
        value = self.values[key]
        return self.fitted_row(key, str(value) if value != "" else "(not set)", value != selfhost.DEFAULTS[key])

    def stepped(self, key: str, delta: int) -> typing.Any:
        value = self.values[key]
        return max(0, value + delta) if isinstance(value, int) else None

    def update_editor(self, key: str) -> bool:
        if not self.query("#int"):
            return False
        self.query_one("#int", Input).value = str(self.values[key])
        return True

    def set_value(self, key: str, value: typing.Any) -> None:
        if self.values[key] != value:
            self.values[key] = value
            self.dirty = True
        self.refresh_row(key)

    # editor

    async def build_editor(self) -> None:
        key = self.editor_key
        value, default = self.values[key], selfhost.DEFAULTS[key]
        widgets: list = [Label(Text(key, style="bold")),
                         Label(Text(f"default: {default if default != '' else '(not set)'}", style="dim"),
                               classes="heading-end")]
        if key in PATH_KEYS:
            widgets.append(PathInput(str(value), id="path",
                                     placeholder="Path. Enter sets, Ctrl+Space completes, Ctrl+B browses"))
            widgets.append(Label("", id="exists"))
        elif isinstance(default, int):
            widgets.append(Input(str(value), type="integer", id="int", placeholder="Number (Enter to set)"))
        else:
            widgets.append(Input(str(value), id="str", placeholder="Text (Enter to set)"))
        widgets.append(Label("", id="error"))
        await self.mount_editor(widgets)
        self.update_exists()
        self.query_one("#doc-text", Static).update(Text(selfhost.DESCRIPTIONS[key] + "\n\n" + APPLY_NOTE))
        self.query_one("#doc", VerticalScroll).scroll_home(animate=False)

    def update_exists(self) -> None:
        if not self.query("#exists"):
            return
        value = self.values[self.editor_key]
        if not value:
            text = Text("not set", style="dim")
        elif os.path.exists(os.path.expanduser(value)):
            text = Text(f"{glyphs.symbol('exists')} exists", style="green")
        else:
            text = Text("x doesn't exist", style="red")
        self.query_one("#exists", Label).update(text)

    @on(Input.Submitted, "#path")
    @editor_event
    def path_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        value = event.value.strip()
        self.set_value(self.editor_key, os.path.expanduser(value) if value else "")
        self.update_exists()
        self.back_to_rows()

    def action_browse(self) -> None:
        if not self.query("#path"):
            return
        key = self.editor_key
        value = os.path.expanduser(self.values[key])
        start = value if os.path.isdir(value) else os.path.dirname(value) if value else os.path.expanduser("~")

        def chosen(path: Path | None) -> None:
            if path is not None:
                self.query_one("#path", PathInput).value = str(path)
                self.set_value(key, str(path))
                self.update_exists()

        self.app.push_screen(FilePicker(f"Choose {key}", [], "selfhost_browse_dir",
                                        start if os.path.isdir(start) else os.path.expanduser("~"),
                                        folders=PATH_KEYS[key], show_all=True), chosen)

    @on(Input.Submitted, "#int")
    @editor_event
    def int_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        try:
            self.set_value(self.editor_key, int(event.value))
        except ValueError:
            self.error("Not a number.")
            return
        self.error("")
        self.back_to_rows()

    @on(Input.Submitted, "#str")
    @editor_event
    def str_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.set_value(self.editor_key, event.value.strip())
        self.back_to_rows()

    async def action_reset(self) -> None:
        if self.editor_key:
            self.set_value(self.editor_key, selfhost.DEFAULTS[self.editor_key])
            await self._build_editor_now()

    def action_save(self) -> None:
        if not self.dirty:
            self.notify("No changes to save")
            return
        values = self.values
        problem = next((f"{key} file not found: {values[key]}" for key in ("cert", "key")
                        if values[key] and not os.path.isfile(values[key])), None)
        if bool(values["cert"]) != bool(values["key"]):
            problem = "Set both cert and key (or neither)."
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


APPLY_NOTE = "Changes apply when the server (re)starts."


SCREEN_CSS = """
.heading { padding: 0 1; text-style: bold; }
#server, #room { padding: 0 1; height: auto; }
#buttons { height: auto; }
#buttons Button { margin-right: 1; }
#console { height: 1fr; border: tall $border-blurred; }
#console:focus { border: tall $border; }
"""


class SelfHostScreen(Screen):
    CSS = SCREEN_CSS
    BINDINGS = [Binding("ctrl+r", "toggle_server", "Start/stop server"), Binding("ctrl+o", "load", "Load game"),
                Binding("ctrl+e", "settings", "Settings"), Binding("ctrl+b", "open_room", "Open room"),
                Binding("ctrl+w", "wake", "Wake room"), Binding("escape", "back", "Back", show=False)]

    def __init__(self, load: str | None = None) -> None:
        super().__init__()
        self.initial_load = load
        self.busy = ""  # what's running in the background (start/stop/load), shown instead of the server state
        self.status: dict[str, typing.Any] | None = None
        self.info: dict[str, typing.Any] | None = None
        self.log_file: str | None = None
        self.log_offset = 0
        self.focus_before: typing.Any = None

    def compose(self) -> ComposeResult:
        yield Label("Server", classes="heading")
        yield Static(id="server")
        with Horizontal(id="buttons"):
            yield Button("Start server", id="toggle", variant="primary")
            yield Button("Load game", id="load")
            yield Button("Settings", id="settings")
            yield Button("Open room", id="open")
            yield Button("Wake room", id="wake")
        yield Label("Room", classes="heading")
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
            self.load(self.initial_load)

    # --- state ---

    def refresh_state(self) -> None:
        self.status = selfhost.server_status()
        self.info = selfhost.room_info()
        config = selfhost.load_config()
        running = bool(self.status and self.status.get("serving"))
        server = self.query_one("#server", Static)
        toggle = self.query_one("#toggle", Button)
        toggle.label = "Stop server" if self.status else "Start server"
        for button in self.query("#buttons Button"):
            button.disabled = bool(self.busy)
        if self.busy:
            server.update(Text(self.busy, style="yellow"))
        elif running:
            url = selfhost.base_url(config, self.status)
            server.update(Text.assemble(
                ("● Running ", "bold green"), (url, f"underline link {url}"),
                f"  (listening on {self.status['bind']}:{self.status['port']})\n",
                ("Keeps running after you leave; stop it here.", "dim")))
        elif self.status:
            server.update(Text("Starting…", style="yellow"))
        else:
            server.update(Text.assemble(("○ Stopped", "bold"), ("  Ctrl+R starts it.", "dim")))
        self.query_one("#room", Static).update(self.room_text(config, running))
        self.query_one("#open", Button).disabled = bool(self.busy) or not (running and self.info)
        self.query_one("#wake", Button).disabled = bool(self.busy) or not self.info
        self.tail_log()

    def room_text(self, config: dict[str, typing.Any], running: bool) -> Text:
        if not self.info:
            return Text("No game loaded. Ctrl+O loads a generated game (.zip / .archipelago).", style="dim")
        info = self.info
        players = ", ".join(f"{name} ({game})" for _, name, game in info["players"])
        text = Text.assemble((info["name"], "bold"), f"  loaded {info['loaded']}\n",
                             f"{len(info['players'])} players: {players}\n")
        state = selfhost.room_state(info)
        address = config["public_address"] or (self.status or {}).get("host_address") or "localhost"
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
        for name, url in selfhost.links(info, base).items():
            text.append_text(Text.assemble(f"{name}: ", (url, f"link {url}"), "\n"))
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
        threading.Thread(target=run, daemon=True).start()

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
            self.in_background("Starting (loading worlds)…", start)

    def action_load(self) -> None:
        if self.busy:
            return
        from aplite.file_picker import FilePicker
        from aplite.webhost import output_folder
        picker = FilePicker("Self-host: pick a generated game", SUFFIXES, "host_dir", output_folder())
        self.app.push_screen(picker, lambda path: path and self.load(str(path)))

    def load(self, path: str) -> None:
        def work() -> str:
            folder = selfhost.load_game(path)
            self.app.call_from_thread(setattr, self, "busy", "Loaded; restarting the server with it…")
            selfhost.stop_server()
            archived = selfhost.replace_current(selfhost.load_config(), folder)
            started = start()
            return " ".join(filter(None, [f"Hosting {Path(path).stem}.", started,
                                          f"Previous room archived in {archived}" if archived else None]))
        self.in_background(f"Loading {Path(path).name}…", work)

    def action_settings(self) -> None:
        def saved(config: dict[str, typing.Any]) -> None:
            message = "Saved."
            if self.info:
                try:
                    selfhost.set_timeout(self.info, int(config["room_timeout"]))
                except Exception as e:
                    message += f" Couldn't set the room timeout: {e}"
            if self.status:
                message += " Restart the server (stop, then start) to apply."
            self.notify(message, timeout=8)
        self.app.push_screen(SettingsScreen(saved))

    def action_open_room(self) -> None:
        if not (self.info and self.status):
            return
        from aplite.desktop import open_path
        url = selfhost.links(self.info, selfhost.base_url(selfhost.load_config(), self.status))["Room"]
        if not open_path(url):
            self.notify(f"No browser opener found; open {url}", timeout=10)

    def action_wake(self) -> None:
        if self.info:
            selfhost.wake(self.info)
            self.refresh_state()

    def action_back(self) -> None:
        """Pushed from Generate GUI: back there (the server keeps running)."""
        if len(self.app.screen_stack) > 1:
            self.app.pop_screen()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        {"toggle": self.action_toggle_server, "load": self.action_load, "settings": self.action_settings,
         "open": self.action_open_room, "wake": self.action_wake}[event.button.id]()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        text = event.value.strip()
        if not text:
            return
        if not self.info:
            self.notify("No game loaded.", severity="warning")
            return
        state = selfhost.room_state(self.info)
        if state and not state["awake"]:
            selfhost.wake(self.info)
        selfhost.send_command(self.info, text)
        self.query_one("#console", RichLog).write(Text(f"> {text}", style="bold"))
        event.input.value = ""


def start() -> str | None:
    selfhost.start_server()
    if selfhost.wait_for_server():
        return None
    raise RuntimeError(f"The server didn't start; see {selfhost.root('server.log')}")


class SelfHostApp(AppliteApp):
    TITLE = "Self-host"
    CSS = SCREEN_CSS  # a default screen's own CSS isn't loaded

    def __init__(self, load: str | None = None) -> None:
        super().__init__()
        self.load = load

    def get_default_screen(self) -> Screen:
        return SelfHostScreen(self.load)


def run(*args: str) -> None:
    SelfHostApp(args[0] if args else None).run()


def component():
    """Launcher entry for this tool (added to the component list by aplite)."""
    from worlds.LauncherComponents import Component, Type
    return Component("Self-host", func=run, component_type=Type.TOOL,
                     description="Host a generated game on this machine with the website's room page, trackers and "
                                 "downloads. The server keeps running in the background.")
