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
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Input, Label, RichLog, Static

import Utils
from aplite import selfhost
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


class SettingsModal(ModalScreen[bool]):
    """selfhost/config.yaml as a form; dismisses True when saved."""
    CSS = """
    SettingsModal { align: center middle; }
    #settings-box { width: 90%; height: 90%; padding: 0 1; border: thick $primary; background: $surface; }
    #settings-fields { height: 1fr; }
    #settings-fields Label { padding: 1 1 0 1; }
    #settings-error { color: $error; padding: 0 1; }
    #settings-buttons { height: auto; align-horizontal: right; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self) -> None:
        super().__init__()
        self.config = selfhost.load_config()

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-box"):
            yield Label(f"Self-host settings ({selfhost.root('config.yaml')}); they apply when the server starts.")
            with VerticalScroll(id="settings-fields"):
                for key, description in selfhost.DESCRIPTIONS.items():
                    yield Label(description)
                    yield Input(str(self.config[key]), id=f"set-{key}",
                                type="integer" if isinstance(selfhost.DEFAULTS[key], int) else "text")
            yield Label("", id="settings-error")
            with Horizontal(id="settings-buttons"):
                yield Button("Save", id="settings-save", variant="primary")
                yield Button("Cancel", id="settings-cancel")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.focus_next()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "settings-save":
            self.save()
        else:
            self.action_cancel()

    def save(self) -> None:
        config = dict(self.config)
        for key in selfhost.DESCRIPTIONS:
            value = self.query_one(f"#set-{key}", Input).value.strip()
            if isinstance(selfhost.DEFAULTS[key], int):
                try:
                    value = int(value)
                except ValueError:
                    self.query_one("#settings-error", Label).update(f"{selfhost.DESCRIPTIONS[key]}: not a number")
                    return
            config[key] = value
        for key in ("cert", "key"):
            if config[key] and not os.path.isfile(os.path.expanduser(config[key])):
                self.query_one("#settings-error", Label).update(f"{key} file not found: {config[key]}")
                return
        if bool(config["cert"]) != bool(config["key"]):
            self.query_one("#settings-error", Label).update("Set both the certificate and the key (or neither).")
            return
        config["cert"], config["key"] = (os.path.expanduser(config[k]) if config[k] else "" for k in ("cert", "key"))
        selfhost.save_config(config)
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


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
        def saved(ok: bool | None) -> None:
            if not ok:
                return
            config = selfhost.load_config()
            if self.info:
                try:
                    selfhost.set_timeout(self.info, int(config["room_timeout"]))
                except Exception:
                    pass
            if self.status:
                self.notify("Saved. Restart the server (Ctrl+R twice) to apply.", timeout=8)
        self.app.push_screen(SettingsModal(), saved)

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
