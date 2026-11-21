"""Multi-Slot Text Client: one text client per slot of a room, on one event loop, in one screen.

Every slot has its own CommonContext and socket with full text, and keeps its own log. The "All slots" view merges
them without duplicates (MultiLog). A session (address, password, slot names, the room's seed) saves to a YAML file
in multisessions/. Design: ignore/claude plan.md, "Multi-slot Text Client"."""
from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import functools
import logging
import os
import sys
import threading
import typing
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import Button, Checkbox, DataTable, Footer, Input, OptionList, RichLog, Static, TabbedContent, \
    TabPane
from textual.widgets.option_list import Option
from textual.widgets.tabbed_content import ContentTab

import Utils
from aplite import colors, glyphs, nav, prefs
from aplite.combo import FilterInput, highlight_first
from aplite.nav import AppliteApp, on_app_thread, terminal_handlers

if typing.TYPE_CHECKING:
    from CommonClient import CommonContext

DEFAULT_DIR = Utils.user_path("multisessions")
STATE_REFRESH = 0.5
"""Seconds between slot list / status / hints refreshes."""
CHECK_TIMEOUT = 15.0
"""Seconds the Add slot dialog waits for a server that neither takes nor refuses a new slot (password dialog not
counted). Refusals and failed connections end the check at once."""
SHUTDOWN_TIMEOUT = 5.0
"""Seconds to wait for the slots' connections to close when leaving."""

SLOT: contextvars.ContextVar[str | None] = contextvars.ContextVar("aplite_slot", default=None)
"""The slot whose tasks are running: each context's tasks start inside its own copy of the contextvars, so log records
from upstream's global loggers carry the slot they came from."""

ARCHIPELAGO_LOGGERS = {"Client", "StreamLog"}
CHAT_TYPES = {"Chat", "ServerChat"}
ITEM_TYPES = {"ItemSend", "ItemCheat"}
ITEM_CLASSES = {"progression": 0b001, "useful": 0b010, "trap": 0b100, "filler": 0}
"""Item classification checkboxes -> ItemSend flags (filler: no flag)."""
BROADCAST_TYPES = {"Chat", "ServerChat", "Join", "Part", "Goal", "Release", "Collect", "Countdown", "TagsChanged",
                   "ItemCheat", "ItemSend"}
"""PrintJSON types the server sends to everyone (broadcast_text_all; ItemSend to the whole team, MultiServer.py
1240-1248): tagged [broadcast] in "All slots" instead of the slot whose copy came first. Hints go only to the
slots involved, command results and the tutorial to one client."""
BROADCAST_TAG = ("[broadcast] ", "dark_orange")
"""Reserved: not one of SLOT_COLORS."""
SLOT_COLORS = ["cyan", "magenta", "green", "yellow", "bright_blue", "bright_red", "bright_cyan", "bright_magenta",
               "bright_green", "bright_yellow"]
"""Slot tag colors in "All slots", by the slot's position in the session."""
TABS: dict[str, typing.Callable[[Line], bool]] = {
    "archipelago": lambda line: line.logger in ARCHIPELAGO_LOGGERS,
    "chat": lambda line: line.kind in CHAT_TYPES,
    "all": lambda line: True,
}
"""Log tab -> which lines it shows (on top of the slot / item filters)."""
PREF_ITEMS = "multislot_items"
PREF_BELL = "multislot_bell"


def item_visible(flags: int | None, enabled: typing.Collection[str]) -> bool:
    if flags is None:
        return True
    if not flags & 0b111:
        return "filler" in enabled
    return any(flags & bit and name in enabled for name, bit in ITEM_CLASSES.items() if bit)


# sessions


@dataclasses.dataclass
class Session:
    address: str = ""
    password: str | None = None
    slots: list[str] = dataclasses.field(default_factory=list)
    seed_name: str | None = None
    """The room's seed, stored on the first successful connect: a room that comes back on another port (and a port
    reused by a stranger's room) is caught before sending Connect."""

    @classmethod
    def read(cls, path: Path) -> Session:
        """Raises ValueError for a file that isn't a session."""
        import yaml
        with open(path, encoding="utf-8-sig") as f:
            doc = yaml.safe_load(f) or {}
        if not isinstance(doc, dict) or not isinstance(doc.get("slots", []), list):
            raise ValueError("not a multi-slot session file")
        return cls(address=str(doc.get("address") or ""), password=doc.get("password") or None,
                   slots=[str(s) for s in doc.get("slots") or []], seed_name=doc.get("seed_name") or None)

    def write(self, path: Path) -> None:
        import yaml
        doc = {"address": self.address, "password": self.password, "slots": list(self.slots),
               "seed_name": self.seed_name}
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Archipelago Lite multi-slot client session\n")
            yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)


# logs


@dataclasses.dataclass(eq=False)
class Line:
    slot: str | None
    """None: not from a slot (aplite's own messages, startup log)."""
    logger: str
    text: Text
    kind: str | None = None
    """PrintJSON type (ItemSend, Chat...), None for other log lines."""
    flags: int | None = None
    """Item flags of an ItemSend / ItemCheat."""
    receiving: int | None = None
    level: int = logging.INFO
    reason: str | None = None
    """For errors: the short reason, without the traceback."""
    merged: bool = True
    """Shown in "All slots": the first copy of a message, not the same broadcast seen by another slot."""


class MultiLog(logging.Handler):
    """Root log handler while the multi-slot client runs: tags records with their slot (SLOT), keeps every line, and
    decides which ones the merged view shows.

    Merging: each message gets a key (a PrintJSON's raw data, or a plain record's logger + text; the text of a
    PrintJSON differs per slot since each colors its own name). The merged view shows a copy whenever one slot has now
    seen that key more often than the merged view has shown it: the same broadcast on N sockets shows once, and a
    real repeat (someone saying "lol" twice) shows twice."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[Line] = []
        self.sink: typing.Callable[[Line], None] | None = None
        self._seen: dict[tuple[str | None, typing.Hashable], int] = {}
        self._shown: dict[typing.Hashable, int] = {}
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "NoStream", False) or getattr(record, "skip_gui", False):
            return
        try:
            msg = self.format(record)
        except Exception:
            self.handleError(record)
            return
        name = record.name.split(".")[0]
        info = getattr(record, "aplite_print", None)
        line = Line(SLOT.get(), name, Text.from_ansi(msg), level=record.levelno)
        if record.levelno >= logging.ERROR:
            exc = record.exc_info[1] if record.exc_info else None
            # upstream raises plain Exceptions with the server's refusal as the message; for network errors
            # (OSError...) its own log message says it better
            line.reason = str(exc) if type(exc) is Exception and str(exc) else record.getMessage()
        if info:
            line.kind, line.flags, line.receiving, key = info
        else:
            key = (name, msg)
        self.add(line, key)

    def add(self, line: Line, key: typing.Hashable | None = None) -> None:
        """Keep a line (key None: always shown merged) and pass it on."""
        with self._lock:
            if key is not None and line.slot is not None:
                seen = self._seen.get((line.slot, key), 0) + 1
                self._seen[line.slot, key] = seen
                line.merged = seen > self._shown.get(key, 0)
                if line.merged:
                    self._shown[key] = seen
            self.lines.append(line)
        if self.sink:
            self.sink(line)

    def forget(self, slot: str, everything: bool = False) -> None:
        """A closed slot: drop its lines and counts. The merged view keeps the copies it showed from it, unless
        `everything` (a slot that was never added: a failed check)."""
        with self._lock:
            self.lines = [line for line in self.lines if line.slot != slot or (line.merged and not everything)]
            self._seen = {k: v for k, v in self._seen.items() if k[0] != slot}
            self._shown = {}  # shown = the most any slot has seen (else others' copies of its messages stay hidden)
            for (_, key), seen in self._seen.items():
                self._shown[key] = max(self._shown.get(key, 0), seen)


# contexts


@functools.cache
def context_class() -> type:
    """SlotContext, built on first use: importing CommonClient loads every world, which the launcher (importing this
    module for its entry) must not pay for."""
    from CommonClient import CommonContext, logger
    from NetUtils import encode

    class SlotContext(CommonContext):
        """The Text Client's context (CommonClient.run_as_textclient's TextContext), for one slot of a session: never
        prompts for the name, asks for a missing room password in the session's one dialog, and stops before Connect
        in a different room."""
        tags = CommonContext.tags | {"TextOnly"}
        game = ""  # empty matches any game
        items_handling = 0b111  # receive all items for /received
        want_slot_data = False

        def __init__(self, owner: MultiClientApp, name: str) -> None:
            super().__init__(None, owner.session.password)
            self.owner = owner
            self.username = name  # also lets upstream auto-reconnect before the first successful connect
            self.slot_name = name
            self.error: str | None = None
            self.closed_count = 0
            """Connections that ended (successful or not): the Add slot check's sign that an attempt is over."""

        async def server_auth(self, password_requested: bool = False) -> None:
            if password_requested and not self.password:
                # aplite's hook (tui.install_password_prompt): the session's shared password dialog
                await CommonContext.server_auth(self, password_requested)
            if not self.owner.room_matches(self.server_seed_name):
                self.refuse("Different room")
                return
            self.auth = self.slot_name
            await self.send_connect(game="")

        def refuse(self, error: str) -> None:
            """Stop this connection without reconnecting. Called from the server loop's own task, which must not
            await its own end (disconnect would): only start closing the socket."""
            self.error = error
            logger.error(f"{self.slot_name}: {error}")
            self.disconnected_intentionally = True
            self.cancel_autoreconnect()
            if self.server and not self.server.socket.closed:
                asyncio.create_task(self.server.socket.close())

        def event_invalid_slot(self) -> None:
            self.error = "No such slot in this room"
            super().event_invalid_slot()

        def event_invalid_game(self) -> None:
            self.error = "Invalid game"
            super().event_invalid_game()

        def handle_connection_loss(self, msg: str) -> None:
            from aplite.tui import PasswordCancelled
            if self.error and not isinstance(sys.exc_info()[1], PasswordCancelled):  # reason shown: no traceback
                logger.error(msg)
            else:
                super().handle_connection_loss(msg)

        async def connection_closed(self) -> None:
            self.closed_count += 1
            await super().connection_closed()

        def on_package(self, cmd: str, args: dict) -> None:
            if cmd == "Connected":
                self.game = self.slot_info[self.slot].game
                self.error = None
                self.owner.slot_connected(self)

        async def disconnect(self, allow_autoreconnect: bool = False) -> None:
            self.game = ""
            await super().disconnect(allow_autoreconnect)

        def on_print_json(self, args: dict) -> None:
            import copy
            data = args["data"]
            item = args.get("item")
            info = (args.get("type"), getattr(item, "flags", None), args.get("receiving"),
                    encode({k: v for k, v in args.items() if k != "cmd"}))
            logging.getLogger("FileLog").info(self.rawjsontotextparser(copy.deepcopy(data)), extra={"NoStream": True})
            logging.getLogger("StreamLog").info(self.jsontotextparser(copy.deepcopy(data)),
                                                extra={"NoFile": True, "aplite_print": info})

    return SlotContext


def connected(ctx: CommonContext) -> bool:
    return bool(ctx.server and ctx.server.socket and not ctx.server.socket.closed)


# UI


class NoSuggestions(Suggester):
    """The input's suggester while no slot is picked; HintsPane asks it for item names."""

    def __init__(self) -> None:
        super().__init__(use_cache=False)

    async def get_suggestion(self, value: str) -> str | None:
        return None

    def names(self, kind: str) -> list[str]:
        return []


class AddSlotModal(ModalScreen[str | None]):
    """A slot name: typed, or picked from the room's players not in the session yet (known once a slot connected)."""
    CSS = """
    AddSlotModal { align: center middle; background: transparent; }
    AddSlotModal > Vertical { width: 60; max-width: 90%; height: auto; max-height: 80%; padding: 0 1;
                              border: thick $primary; background: $surface; }
    AddSlotModal Static { padding: 0 0 1 0; }
    AddSlotModal OptionList { height: auto; max-height: 15; border: none; }
    AddSlotModal DialogButtons { margin-top: 0; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, players: list[tuple[str, str]], check: typing.Callable[[str], typing.Awaitable[str | None]],
                 connecting: bool = False) -> None:
        """`check(name)`: tries the slot (adds it if the server takes it), returns an error or None. `connecting`:
        opened by connecting a session without slots, so it's the connection's next step: the password dialog's
        Disconnect / Connect buttons instead of Cancel / Add."""
        super().__init__()
        self.players = players
        self.check = check
        self.connecting = connecting
        self.checking: typing.Any = None

    def compose(self) -> ComposeResult:
        with Vertical():
            hint = " (Up/Down pick a player of this room)" if self.players else ""
            yield Static(f"[b]{'Connect as' if self.connecting else 'Add slot'}[/b]: the slot name{hint}")
            yield FilterInput(placeholder="Slot name", id="slot-name", target="#players")
            yield OptionList(id="players")
            yield Static(id="slot-status")
            with nav.DialogButtons():
                yield Button("Disconnect" if self.connecting else "Cancel", id="slot-cancel")
                yield Button("Connect" if self.connecting else "Add", id="slot-add", variant="primary")

    def on_mount(self) -> None:
        self.fill("")
        self.query_one("#slot-status").display = False
        self.query_one("#slot-name").focus()

    def status(self, text: Text | str | None) -> None:
        status = self.query_one("#slot-status", Static)
        status.display = text is not None
        status.update(text or "")

    def fill(self, query: str) -> None:
        options = self.query_one("#players", OptionList)
        options.clear_options()
        query = query.strip().casefold()
        matches = [(name, game) for name, game in self.players if query in name.casefold()]
        options.add_options([Option(Text.assemble(name, (f"  {game}", "dim")), id=name) for name, game in matches])
        options.display = bool(matches)
        if query:
            highlight_first(options)
        else:
            options.highlighted = None  # Enter adds what was typed until Up/Down picks one

    @on(Input.Changed, "#slot-name")
    def changed(self, event: Input.Changed) -> None:
        event.stop()
        self.fill(event.value)

    @on(Input.Submitted, "#slot-name")
    @on(Button.Pressed, "#slot-add")
    def submitted(self, event: Input.Submitted | Button.Pressed) -> None:
        event.stop()
        options = self.query_one("#players", OptionList)
        option = options.get_option_at_index(options.highlighted) if options.highlighted is not None else None
        name = option.id if option and options.display else self.query_one("#slot-name", Input).value.strip()
        if name:
            self.try_name(name)
        else:
            self.query_one("#slot-name").focus()

    @on(OptionList.OptionSelected, "#players")
    def picked(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.try_name(event.option.id)

    def try_name(self, name: str) -> None:
        """Check the name with the server (in a worker: the dialog stays usable, Cancel abandons the check)."""
        if self.checking and self.checking.is_running:
            return
        self.status(Text(f"Checking {name}…", style="dim"))
        self.query_one("#slot-add", Button).disabled = True

        async def check() -> None:
            error = await self.check(name)
            if error is None:
                self.dismiss(name)
                return
            self.status(Text(error, style="red"))
            self.query_one("#slot-add", Button).disabled = False
            self.query_one("#slot-name").focus()
        self.checking = self.run_worker(check(), exclusive=True)

    @on(Button.Pressed, "#slot-cancel")
    def cancel_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()

    def action_cancel(self) -> None:
        if self.checking and self.checking.is_running:
            self.checking.cancel()  # the app's check drops the half-added slot
        self.dismiss(None)


class MultiClientApp(AppliteApp):
    TITLE = "Multi-Slot Client"
    CSS = """
    #connect-bar { height: 1; background: $panel; }
    #address { width: 1fr; max-width: 40; }
    #connect { min-width: 14; }
    #status { width: 1fr; padding: 0 1; }
    #body { height: 1fr; }
    #side { width: 30%; min-width: 30; max-width: 60; border: round $border-blurred; }
    #side:focus-within { border: round $border; }
    #slots { height: 1fr; border: none; text-wrap: nowrap; text-overflow: ellipsis; }
    #side-buttons { height: 1; }
    #side-buttons Button { width: 1fr; min-width: 0; }
    #main { width: 1fr; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0; }
    RichLog, DataTable { height: 1fr; }
    #filters { height: 1; overflow: hidden; }
    #filters Checkbox { width: auto; }
    #filters Static { width: auto; padding: 0 1; }
    """
    BINDINGS = [Binding("ctrl+n", "switch_tab(1)", "Next tab"), Binding("ctrl+b", "switch_tab(-1)", "Prev tab"),
                Binding("ctrl+g", "toggle_slots", "Slots"), Binding("ctrl+y", "add_slot", "Add slot"),
                Binding("ctrl+r", "close_slot", "Close slot"), Binding("ctrl+o", "load", "Load"),
                Binding("ctrl+s", "save", "Save"), Binding("ctrl+t", "focus_address", "Address", show=False),
                Binding("ctrl+f", "focus_search", "Search", show=False)]

    def __init__(self, log: MultiLog) -> None:
        super().__init__()
        self.log_handler = log
        self.session = Session()
        self.contexts: dict[str, CommonContext] = {}
        self.vars: dict[str, contextvars.Context] = {}
        self.processors: dict[str, typing.Any] = {}
        self.pending: set[str] = set()
        """Slots being checked by the Add slot dialog: connected, but not listed or in the session yet."""
        self.selected: str | None = None
        self.loaded_path: Path | None = None
        self.dirty = False
        self.room_mismatch = False
        self.confirm = nav.Confirm()
        self.items: set[str] = set(prefs.get(PREF_ITEMS) or ITEM_CLASSES)
        self._hints_snapshot: object = None
        self._hint_rows: list[tuple[dict, int, CommonContext | None]] = []
        self._slot_rows: list[str | None] = []
        self._log_width = 0
        """Width of the log area the logs were last written for: RichLog never re-wraps old lines, so a different
        width (window resized; the slot pane is a share of it) rewrites them."""

    # layout

    def compose(self) -> ComposeResult:
        from aplite.tui import CommandInput, HintsPane, SelectableLog
        with Horizontal(id="connect-bar"):
            yield Input(id="address", compact=True, placeholder="address:port")
            yield Button("Connect", id="connect", compact=True)
            yield Static(id="status")
        with Horizontal(id="body"):
            with Vertical(id="side"):
                yield OptionList(id="slots")
                with Horizontal(id="side-buttons"):
                    yield Button("Add", id="add", compact=True)
                    yield Button("Close", id="close", compact=True)
            with Vertical(id="main"):
                with TabbedContent(id="tabs"):
                    with TabPane("Archipelago", id="tab-archipelago"):
                        yield SelectableLog(id="log-archipelago", wrap=True, min_width=20)
                    with TabPane("Chat", id="tab-chat"):
                        yield SelectableLog(id="log-chat", wrap=True, min_width=20)
                    with TabPane("All", id="tab-all"):
                        yield SelectableLog(id="log-all", wrap=True, min_width=20)
                    with TabPane("Hints", id="tab-hints"):
                        yield HintsPane()
                with Horizontal(id="filters"):
                    yield Static("Items:")
                    for name in ITEM_CLASSES:
                        yield Checkbox(name, name in self.items, id=f"items-{name}", compact=True)
                    yield Static("│")
                    bell = Checkbox("Bell", bool(prefs.get(PREF_BELL)), id="bell", compact=True)
                    bell.tooltip = "Ring the terminal bell when one of your slots receives a progression item"
                    yield bell
        yield CommandInput(placeholder="", suggester=NoSuggestions())
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#hints", DataTable).add_columns("Receiving", "Item", "Finding", "Location", "Entrance",
                                                        "Status")
        self.refresh_slot_list()
        self.select(None)
        self.log_handler.sink = lambda line: on_app_thread(self, self.route, line)
        from aplite.tui import PasswordAsker
        self.passwords = PasswordAsker(self)
        self.set_interval(STATE_REFRESH, self.poll_state)
        self.refresh_state()
        self.info("Load a session (Ctrl+O), or enter the server address above and add slots (Ctrl+Y).")
        self.query_one("#address").focus()

    def on_unmount(self) -> None:
        self.passwords.close()

    def info(self, text: str, style: str = "") -> None:
        self.log_handler.add(Line(None, "Client", Text(text, style=style)))

    # logs

    def visible(self, line: Line) -> bool:
        if line.kind in ITEM_TYPES and not item_visible(line.flags, self.items):
            return False
        if self.selected is None:
            return line.merged or line.slot is None
        return line.slot == self.selected or line.slot is None

    def render_line(self, line: Line) -> Text:
        if self.selected is not None or line.slot is None:
            return line.text
        if line.kind in BROADCAST_TYPES:
            return Text.assemble(BROADCAST_TAG, line.text)
        names = list(self.contexts) or [line.slot]
        color = SLOT_COLORS[names.index(line.slot) % len(SLOT_COLORS)] if line.slot in names else "dim"
        return Text.assemble((f"[{line.slot}] ", color), line.text)

    def route(self, line: Line) -> None:
        try:
            if line.merged and line.kind in ITEM_TYPES and line.flags and line.flags & 0b001:
                self.maybe_bell(line)
            if not self.visible(line):
                return
            self.write_line(line)
        except NoMatches:  # closing
            pass

    def write_line(self, line: Line) -> None:
        text = self.render_line(line)
        for tab, shows in TABS.items():
            if shows(line):
                self.query_one(f"#log-{tab}", RichLog).write(text)

    def view(self, tab: str) -> list[Text]:
        """What a log tab shows now (as rebuild_logs would write it)."""
        return [self.render_line(line) for line in list(self.log_handler.lines)
                if self.visible(line) and TABS[tab](line)]

    def rebuild_logs(self) -> None:
        self._log_width = self.query_one("#tabs").size.width
        for log in self.query(RichLog):
            log.clear()
        for line in list(self.log_handler.lines):
            if self.visible(line):
                self.write_line(line)

    def maybe_bell(self, line: Line) -> None:
        if not prefs.get(PREF_BELL):
            return
        own = {ctx.slot for ctx in self.contexts.values() if ctx.slot}
        if line.receiving in own:
            self.bell()

    @on(Checkbox.Changed)
    def checkbox_changed(self, event: Checkbox.Changed) -> None:
        event.stop()
        if event.checkbox.id == "bell":
            prefs.put(PREF_BELL, event.value)
            return
        name = (event.checkbox.id or "").removeprefix("items-")
        if name in ITEM_CLASSES:
            self.items.add(name) if event.value else self.items.discard(name)
            prefs.put(PREF_ITEMS, [n for n in ITEM_CLASSES if n in self.items])
            self.rebuild_logs()

    # slots

    def slot_vars(self, name: str) -> contextvars.Context:
        context = contextvars.copy_context()
        context.run(SLOT.set, name)
        return context

    def add_slot(self, name: str, connect: bool = True, pending: bool = False) -> None:
        """Add a slot without checking it (a loaded session's; the dialog checks first: try_slot)."""
        if name in self.contexts:
            self.notify(f"{name} is already in this session.", severity="warning")
            return
        from aplite import tui
        context = self.slot_vars(name)
        ctx = context.run(context_class(), self, name)
        setattr(ctx, tui.RUNNING, True)  # aplite's fallback must not start a TUI for it
        ctx.aplite_ask_password = self.ask_password
        ctx.jsontotextparser = colors.make_parser(ctx)  # role markers for RoleFilter
        self.contexts[name] = ctx
        self.vars[name] = context
        self.processors[name] = ctx.command_processor(ctx)
        if pending:
            self.pending.add(name)
        elif name not in self.session.slots:
            self.session.slots.append(name)
        self.refresh_slot_list()
        if connect:
            self.connect_slot(name)

    def connect_slot(self, name: str) -> None:
        address = self.session.address
        if not address:
            return
        ctx = self.contexts[name]
        ctx.error = None
        ctx.password = self.session.password
        ctx.server_address = address  # upstream auto-reconnects to this even if the first try fails
        self.vars[name].run(asyncio.create_task, ctx.connect(address), name=f"connect {name}")

    def disconnect_slot(self, name: str) -> None:
        ctx = self.contexts[name]
        self.vars[name].run(asyncio.create_task, ctx.disconnect(), name=f"disconnect {name}")

    def close_slot(self, name: str) -> None:
        ctx = self.contexts.pop(name)
        self.vars.pop(name)
        self.processors.pop(name)
        was_pending = name in self.pending
        self.pending.discard(name)
        if name in self.session.slots:
            self.session.slots.remove(name)
        asyncio.create_task(self.close_context(ctx), name=f"close {name}")
        self.log_handler.forget(name, everything=was_pending)
        if was_pending:
            self.rebuild_logs()
        if self.selected == name:
            self.selected = None
        self.refresh_slot_list()
        self.select(self.selected)

    @staticmethod
    async def close_context(ctx: CommonContext) -> None:
        try:
            await asyncio.wait_for(ctx.shutdown(), SHUTDOWN_TIMEOUT)
        except (asyncio.TimeoutError, Exception):
            pass

    def room_names(self) -> list[str] | None:
        """Every player slot name of the room, once any slot has connected; else None."""
        from NetUtils import SlotType
        for ctx in self.contexts.values():
            if ctx.slot_info:
                return [info.name for slot, info in ctx.slot_info.items() if slot and info.type == SlotType.player]
        return None

    async def try_slot(self, name: str) -> str | None:
        """The Add slot dialog's check: None if `name` is now a connected slot of the session, else why not. With a
        slot already in, the room's player list answers; otherwise it connects (unlisted) and waits for the
        server's answer. Cancelled (the dialog's Cancel): drops the half-added slot."""
        if name in self.contexts and name not in self.pending:
            return f"{name} is already in this session."
        names = self.room_names()
        if names is not None:
            if name not in names:
                near = next((n for n in names if n.casefold() == name.casefold()), None)
                if near in self.contexts:
                    return f"{near} is already in this session."
                return f"No slot named {name} in this room" + (f": did you mean {near}?" if near else ".")
            self.add_slot(name)
            return None
        if name in self.pending:  # an earlier check still running (can't happen: one check at a time)
            return "Already checking it."
        self.add_slot(name, pending=True)
        ctx = self.contexts[name]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CHECK_TIMEOUT
        try:
            while True:
                await asyncio.sleep(0.1)
                if ctx.slot:
                    self.pending.discard(name)
                    self.session.slots.append(name)
                    self.refresh_slot_list()
                    return None
                error = None
                if ctx.error or ctx.closed_count or ctx.autoreconnect_task:  # the attempt ended without Connected
                    error = ctx.error or self.last_error(name) or f"Could not connect to {self.session.address}."
                elif self.passwords.pending and not self.passwords.pending.done():
                    deadline = loop.time() + CHECK_TIMEOUT  # the password dialog is up: wait for it
                elif loop.time() > deadline:
                    error = "No answer from server."
                if error:
                    self.close_slot(name)
                    return error
        except asyncio.CancelledError:
            if name in self.pending:
                self.close_slot(name)
            raise

    def last_error(self, name: str) -> str | None:
        """The newest error a slot logged, short (for the Add slot dialog)."""
        line = next((line for line in reversed(self.log_handler.lines)
                     if line.slot == name and line.level >= logging.ERROR), None)
        return line and (line.reason or line.text.plain.splitlines()[0])

    def close_all(self) -> None:
        for name in list(self.contexts):
            self.close_slot(name)

    async def ask_password(self, ctx: CommonContext, wrong: bool) -> str | None:
        """The room password for a slot: the session's, else one dialog shared by every slot asking meanwhile.
        Asked again (wrong) with the password this slot already sent: a new dialog, unless another slot's dialog
        already replaced it. None (Disconnect): this slot stops."""
        from aplite.tui import password_message
        sent = getattr(ctx, "aplite_password_sent", None)
        if self.session.password and (not wrong or self.session.password != sent):
            password = self.session.password
        else:
            password = await self.passwords.ask(password_message(ctx, wrong))
        ctx.aplite_password_sent = password
        if not password:
            ctx.error = "No password given"
        elif password != self.session.password:
            self.session.password = password
            self.mark_dirty()
        return password

    def slot_connected(self, ctx: CommonContext) -> None:
        """A slot logged in: remember the room's seed if the session has none yet."""
        if self.session.seed_name is None and ctx.server_seed_name:
            self.session.seed_name = ctx.server_seed_name
            if self.loaded_path:
                self.mark_dirty()
        self.room_mismatch = False

    def room_matches(self, seed_name: str | None) -> bool:
        if self.session.seed_name and seed_name and seed_name != self.session.seed_name:
            self.room_mismatch = True
            return False
        return True

    def room_players(self) -> list[tuple[str, str]]:
        """(name, game) of the room's players not in the session, from any slot that has connected."""
        from NetUtils import SlotType
        for ctx in self.contexts.values():
            if ctx.slot_info:
                return [(info.name, info.game) for slot, info in sorted(ctx.slot_info.items())
                        if slot and info.type == SlotType.player and info.name not in self.contexts]
        return []

    def mark_dirty(self) -> None:
        self.dirty = True
        self.refresh_side_title()

    # slot list

    def slot_prompt(self, name: str | None) -> Text:
        if name is None:
            return Text("All slots", style="bold")
        ctx = self.contexts[name]
        color = SLOT_COLORS[list(self.contexts).index(name) % len(SLOT_COLORS)]
        text = Text()
        listed = [n for n in self.contexts if n not in self.pending]
        name_width = max(map(len, listed), default=len(name))
        width = self.checks_width()  # name, then checks right-aligned in a column, then the game (cut first)
        if ctx.error:
            text.append(f"{glyphs.symbol('off')} ", style="red").append(name, style=color)
            text.append(f"  {ctx.error}", style="red")
            return text
        if ctx.slot:
            text.append(f"{glyphs.symbol('on')} ", style=colors.style("connected"))
        else:
            busy = ctx.server_task or ctx.autoreconnect_task
            text.append(f"{glyphs.symbol('off')} ", style="yellow" if busy else colors.style("not_connected"))
        text.append(f"{name:<{name_width}}", style=color)
        if ctx.slot:
            if width:
                text.append(f"  {self.checks(ctx):>{width}}")
            text.append(f"  {ctx.game}", style="dim")
        elif ctx.autoreconnect_task:
            text.append("  reconnecting", style="dim")
        elif ctx.server_task:
            text.append("  connecting", style="dim")
        return text

    @staticmethod
    def checks(ctx: CommonContext) -> str:
        total = ctx.total_locations if ctx.slot else None
        return f"{len(ctx.checked_locations)}/{total}" if total else ""

    def checks_width(self) -> int:
        return max((len(self.checks(ctx)) for name, ctx in self.contexts.items() if name not in self.pending),
                   default=0)

    def refresh_slot_list(self) -> None:
        slots = self.query_one("#slots", OptionList)
        rows = [None, *(name for name in self.contexts if name not in self.pending)]
        if rows != self._slot_rows:
            self._slot_rows = rows
            slots.clear_options()
            slots.add_options([Option(self.slot_prompt(name)) for name in rows])
            slots.highlighted = rows.index(self.selected) if self.selected in rows else 0
        else:
            for index, name in enumerate(rows):
                slots.replace_option_prompt_at_index(index, self.slot_prompt(name))
        self.query_one("#close", Button).disabled = self.selected is None
        self.refresh_side_title()

    def refresh_side_title(self) -> None:
        name = self.loaded_path.stem if self.loaded_path else "new session"
        self.query_one("#side").border_title = f"{name}{' *' if self.dirty else ''}"

    @on(OptionList.OptionHighlighted, "#slots")
    def slot_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        index = event.option_index
        name = self._slot_rows[index] if 0 <= index < len(self._slot_rows) else None
        if name != self.selected:
            self.select(name)

    def select(self, name: str | None) -> None:
        """Show one slot (its log, its hints; the input sends as it), or None: all slots merged, input off."""
        from aplite.tui import CommandInput, CommandSuggester
        self.selected = name
        command = self.query_one(CommandInput)
        if name is None:
            command.disabled = True
            command.suggester = NoSuggestions()
            command.placeholder = "Pick a slot on the left to chat or send commands as it."
        else:
            command.disabled = False
            command.suggester = CommandSuggester(self.contexts[name])
            command.placeholder = self.command_placeholder(name)
        self.query_one("#close", Button).disabled = name is None
        self._hints_snapshot = None
        self.rebuild_logs()
        self.refresh_hints()

    def command_placeholder(self, name: str) -> str:
        game = self.contexts[name].game
        return f"{name}{f' ({game})' if game else ''}: /help and !help list commands; tab to auto-complete"

    # status

    def poll_state(self) -> None:
        try:
            self.refresh_state()
        except NoMatches:  # the timer can fire while the app closes
            pass

    def refresh_state(self) -> None:
        if self.selected is not None:  # the game arrives with the connection
            from aplite.tui import CommandInput
            self.query_one(CommandInput).placeholder = self.command_placeholder(self.selected)
        if self.query_one("#tabs").size.width != self._log_width:
            self.rebuild_logs()
        self.refresh_slot_list()
        ctxs = [ctx for name, ctx in self.contexts.items() if name not in self.pending]
        active = any(connected(ctx) or ctx.server_task or ctx.autoreconnect_task for ctx in ctxs)
        self.query_one("#connect", Button).label = "Disconnect" if active else "Connect"
        status = Text()
        if self.room_mismatch:
            status.append("Different room at this address (not this session's seed): fix the address.",
                          style="red")
        elif not ctxs:
            status.append("No slots yet: Ctrl+Y adds one.", style="dim")
        else:
            logged_in = sum(bool(ctx.slot) for ctx in ctxs)
            role = "connected" if logged_in == len(ctxs) else "not_connected"
            status.append(f"{glyphs.symbol('on' if logged_in else 'off')} ", style=colors.style(role))
            status.append(f"{logged_in}/{len(ctxs)} connected")
            if self.selected:
                ctx = self.contexts[self.selected]
                if ctx.slot and ctx.hint_points is not None:
                    status.append(f"  │  {self.selected}: {ctx.hint_points} hint points")
        self.query_one("#status", Static).update(status)
        self.refresh_hints()

    # hints (merged across slots, or the selected slot's)

    def hint_sources(self) -> list[CommonContext]:
        if self.selected is not None:
            return [self.contexts[self.selected]]
        return [ctx for name, ctx in self.contexts.items() if name not in self.pending]

    def refresh_hints(self) -> None:
        from aplite.tui import hint_status_display, read_hints
        from NetUtils import HintStatus
        sources = self.hint_sources()
        snapshot = [(id(ctx), [dict(h) for h in read_hints(ctx)]) for ctx in sources]
        if snapshot == self._hints_snapshot:
            return
        self._hints_snapshot = snapshot

        def status_of(hint: dict) -> int:
            if "status" in hint:
                return hint["status"]
            return HintStatus.HINT_FOUND if hint["found"] else HintStatus.HINT_UNSPECIFIED

        merged: dict[tuple[int, int], tuple[dict, CommonContext]] = {}
        for ctx in sources:  # a hint between two own slots arrives on both: keep the receiver's copy
            for hint in read_hints(ctx):
                key = (hint["finding_player"], hint["location"])
                if key not in merged or ctx.slot_concerns_self(hint["receiving_player"]):
                    merged[key] = (hint, ctx)
        names, roles, order = hint_status_display()
        table = self.query_one("#hints", DataTable)
        cursor, scroll = table.cursor_row, table.scroll_y
        table.clear()
        self._hint_rows = []
        for hint, ctx in sorted(merged.values(), key=lambda pair: order.get(status_of(pair[0]), 0), reverse=True):
            def render(node: dict) -> Text:
                return Text.from_ansi(ctx.jsontotextparser.handle_node(node))
            status = status_of(hint)
            status_text = Text(names.get(status, "Unknown"),
                               style=colors.style(roles[status]) if status in roles else "red")
            editable = status != HintStatus.HINT_FOUND and ctx.slot_concerns_self(hint["receiving_player"])
            if editable:
                status_text.stylize("underline")
            table.add_row(
                render({"type": "player_id", "text": hint["receiving_player"]}),
                render({"type": "item_id", "text": hint["item"], "flags": hint["item_flags"],
                        "player": hint["receiving_player"]}),
                render({"type": "player_id", "text": hint["finding_player"]}),
                render({"type": "location_id", "text": hint["location"], "player": hint["finding_player"]}),
                Text(hint["entrance"], style=colors.style("entrance")) if hint["entrance"] else Text("Vanilla"),
                status_text,
                key=str(len(self._hint_rows)),
            )
            self._hint_rows.append((hint, status, ctx if editable else None))
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1), animate=False)
            table.scroll_y = scroll

    @on(DataTable.RowSelected, "#hints")
    def hint_selected(self, event: DataTable.RowSelected) -> None:
        from aplite.tui import HintStatusModal
        hint, status, ctx = self._hint_rows[int(event.row_key.value)]
        if ctx is None:
            self.notify("Only hints for your own slots' unfound items can be changed.", severity="warning")
            return

        def chosen(new_status: int | None) -> None:
            if new_status is not None and new_status != status:
                ctx.update_hint(hint["location"], hint["finding_player"], new_status)

        self.push_screen(HintStatusModal(status), chosen)

    # connection row

    def apply_connection(self) -> bool:
        """Take the row's address into the session. A new address forgets the stored seed (the next
        successful connect stores the new room's). False if there is no address."""
        address = self.query_one("#address", Input).value.strip().removeprefix("/connect ")
        if not address:
            self.notify("Enter the server address first.", severity="warning")
            self.query_one("#address").focus()
            return False
        if address != self.session.address:
            self.session.address = address
            self.session.seed_name = None
            self.mark_dirty()
        self.room_mismatch = False
        return True

    @on(Input.Submitted, "#address")
    def connection_submitted(self, event: Input.Submitted) -> None:
        """Enter in the row (re)connects every slot with what it says."""
        event.stop()
        if self.apply_connection():
            for name in self.contexts:
                self.connect_slot(name)
            self.focus_after_connect()

    @on(Button.Pressed, "#connect")
    def connect_pressed(self, event: Button.Pressed) -> None:
        if str(event.button.label) == "Disconnect":
            for name in self.contexts:
                self.disconnect_slot(name)
            return
        if self.apply_connection():
            if not self.contexts:
                self.notify("Add a slot to connect (Ctrl+Y).")
            for name in self.contexts:
                self.connect_slot(name)
            self.focus_after_connect()

    def focus_after_connect(self) -> None:
        if not self.contexts:
            self.open_add_slot(connecting=True)
        else:
            self.query_one("#slots").focus()

    def action_focus_address(self) -> None:
        self.query_one("#address").focus()

    # actions

    def action_switch_tab(self, step: int) -> None:
        tabs = self.query_one(TabbedContent)
        ids = [ContentTab.sans_prefix(tab.id) for tab in tabs.query(ContentTab)]
        tabs.active = ids[(ids.index(tabs.active) + step) % len(ids)]

    def action_toggle_slots(self) -> None:
        side = self.query_one("#side")
        slots = self.query_one("#slots")
        if side.display and slots.has_focus:
            side.display = False
            self.action_focus_command()
        else:
            side.display = True
            slots.focus()

    def action_focus_command(self) -> None:
        from aplite.tui import CommandInput
        command = self.query_one(CommandInput)
        if not command.disabled:
            command.focus()

    def action_focus_search(self) -> None:
        tabs = self.query_one(TabbedContent)
        tabs.active = "tab-hints"
        self.query_one("#hint-input").focus()

    @on(Button.Pressed, "#add")
    def action_add_slot(self) -> None:
        self.open_add_slot()

    def open_add_slot(self, connecting: bool = False) -> None:
        if not self.session.address and not self.apply_connection():
            return

        def chosen(name: str | None) -> None:
            if name:  # checked and added by try_slot
                self.mark_dirty()
                self.select_row(name)
                self.query_one("#slots").focus()
        self.push_screen(AddSlotModal(self.room_players(), self.try_slot, connecting), chosen)

    def select_row(self, name: str | None) -> None:
        slots = self.query_one("#slots", OptionList)
        if name in self._slot_rows:
            slots.highlighted = self._slot_rows.index(name)
        self.select(name)

    @on(Button.Pressed, "#close")
    def action_close_slot(self) -> None:
        name = self.selected
        if name is None:
            self.notify("Pick the slot to close first.", severity="warning")
            return
        if not self.confirm(self, ("close", name), f"Close {name}? It disconnects and its log goes. Press Ctrl+R "
                                                   f"(or Close) again to close it."):
            return
        self.close_slot(name)
        self.mark_dirty()

    def on_key(self, event) -> None:
        if event.key == "delete" and self.focused is self.query_one("#slots") and self.selected:
            event.stop()
            self.action_close_slot()

    # sessions

    def action_load(self) -> None:
        from aplite.file_picker import FilePicker
        if self.dirty and not self.confirm(self, "load", "Unsaved session changes. Loading replaces them: press "
                                                         "Ctrl+O again to load anyway, or Ctrl+S to save first."):
            return
        os.makedirs(DEFAULT_DIR, exist_ok=True)
        self.push_screen(FilePicker("Load session", [".yaml", ".yml"], "multisession_dir", DEFAULT_DIR),
                         self.load_file)

    def load_file(self, path: Path | None) -> None:
        if path is None:
            return
        try:
            session = Session.read(path)
        except Exception as e:
            self.notify(f"Could not read {path.name}: {e}", severity="error", timeout=10)
            return
        prefs.put("multisession_dir", str(path.parent))
        self.close_all()
        self.session = Session(session.address, session.password, [], session.seed_name)
        self.loaded_path = path
        self.room_mismatch = False
        self.query_one("#address", Input).value = session.address
        for name in dict.fromkeys(session.slots):
            self.add_slot(name)
        self.dirty = False
        self.refresh_side_title()
        self.select_row(None)
        self.query_one("#slots").focus()
        self.info(f"Loaded session {path.name}" + (": connecting" if session.slots and session.address else ""))

    def action_save(self) -> None:
        from aplite.options_creator import SaveScreen
        folder = prefs.get("multisession_dir") or DEFAULT_DIR
        default = str(self.loaded_path) if self.loaded_path else os.path.join(folder, "session.yaml")
        self.push_screen(SaveScreen(default, "Save session as"), self.save_file)

    def save_file(self, path: Path | None) -> None:
        if path is None:
            return
        self.session.address = self.query_one("#address", Input).value.strip() or self.session.address
        try:
            self.session.write(path)
        except OSError as e:
            self.notify(f"Could not save: {e}", severity="error", timeout=10)
            return
        prefs.put("multisession_dir", str(path.parent))
        self.loaded_path = path
        self.dirty = False
        self.refresh_side_title()
        self.notify(f"Saved {path}")

    def can_leave(self, step: str) -> bool:
        if self.dirty and len(self.screen_stack) == 1:
            return self.confirm(self, "leave", "Unsaved session changes: press again to leave anyway, or Ctrl+S to "
                                               "save first.")
        return super().can_leave(step)

    # input

    @on(Input.Submitted, "CommandInput")
    def command_submitted(self, event: Input.Submitted) -> None:
        from aplite.tui import CommandInput
        event.stop()
        text = event.value.strip()
        event.input.value = ""
        if text:
            self.query_one(CommandInput).remember(text)
            self.run_command(text)

    def run_command(self, text: str) -> None:
        """As the selected slot (HintsPane's new-hint box calls this too)."""
        name = self.selected
        if name is None:
            self.notify("Pick a slot on the left first.", severity="warning")
            return
        if text.startswith(("/", "!")):
            self.log_handler.add(Line(name, "Client", Text(text, style=colors.style("echo"))))
        try:
            self.vars[name].run(self.processors[name], text)
        except Exception as e:
            self.vars[name].run(logging.getLogger("Client").exception, e)


async def main() -> None:
    from aplite import tui
    root = logging.getLogger()
    handler = MultiLog()
    for name, msg in tui.log_buffer.records:  # startup messages
        handler.add(Line(None, name, Text.from_ansi(msg)))
    removed = [h for h in terminal_handlers(root) if h is not handler] + [tui.log_buffer]
    for h in removed:
        root.removeHandler(h)
    root.addHandler(handler)
    app = MultiClientApp(handler)
    try:
        await app.run_async()
    finally:
        handler.sink = None
        contexts = list(app.contexts.values())
        if contexts:
            await asyncio.gather(*(MultiClientApp.close_context(ctx) for ctx in contexts))
        root.removeHandler(handler)
        for h in removed:
            root.addHandler(h)


def run(*args: str) -> None:  # a component func: the launcher may pass args, which it doesn't use
    asyncio.run(main())


def component():
    """Launcher entry (added to the component list by aplite)."""
    from worlds.LauncherComponents import Component, Type
    return Component("Text Client (Multi-Slot)", func=run, component_type=Type.CLIENT,
                     description="Several slots of one room in one text client: a merged log, per-slot logs, "
                                 "hints across slots. Sessions save the slots and address.")
