"""Textual replacement for CommonClient.console_loop. ctx.ui stays None so kivy-only client code is skipped."""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import typing

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.suggester import Suggester
from textual.widgets.tabbed_content import ContentTab
from textual.widgets import DataTable, Footer, Input, RichLog, Static, TabbedContent, TabPane

from aplite import prefs

if typing.TYPE_CHECKING:
    from CommonClient import CommonContext

ARCHIPELAGO_LOGGERS = {"Client", "StreamLog"}
"""Top-level logger names shown in the Archipelago tab (StreamLog carries server messages)."""
NO_TAB_LOGGERS = ARCHIPELAGO_LOGGERS | {"root", "FileLog", "websockets", "asyncio", "textual", "performance",
                                        "Benchmark", "Worlds"}
"""Top-level logger names that never get their own tab; anything else that logs gets one (e.g. FactorioServer)."""


class LogBuffer(logging.Handler):
    """Root log handler; keeps history so records logged before the app mounts can be replayed."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[tuple[str, str]] = []
        self.sink: typing.Callable[[str, str], None] | None = None

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "NoStream", False) or getattr(record, "skip_gui", False):
            return
        try:
            msg = self.format(record)
        except Exception:
            self.handleError(record)
            return
        name = record.name.split(".")[0]
        self.records.append((name, msg))
        if self.sink:
            self.sink(name, msg)


log_buffer = LogBuffer()


def install_log_handler() -> None:
    """Route root logging to the TUI instead of the terminal. Safe to call repeatedly (init_logging wipes handlers)."""
    root = logging.getLogger()
    for handler in root.handlers[:]:
        if type(handler) is logging.StreamHandler and handler.stream in (sys.stdout, sys.stderr):
            root.removeHandler(handler)
    if log_buffer not in root.handlers:
        root.addHandler(log_buffer)


class CommandSuggester(Suggester):
    """Inline completion for /local and !server commands, and item/location names after !hint / !hint_location."""

    def __init__(self, ctx: CommonContext) -> None:
        super().__init__(use_cache=False, case_sensitive=False)
        self.ctx = ctx
        from MultiServer import ClientMessageProcessor
        self.local = sorted(f"/{name}" for name in ctx.command_processor.commands)
        self.server = sorted(f"!{name}" for name in ClientMessageProcessor.commands)
        self._names: dict[tuple[str, str], list[str]] = {}

    def names(self, kind: str) -> list[str]:
        game = self.ctx.game
        if not game:
            return []
        if (kind, game) not in self._names:
            lookup = self.ctx.item_names if kind == "item" else self.ctx.location_names
            names = sorted(lookup[game].values(), key=lambda n: (len(n), n))
            if not names:
                return []  # data package not loaded yet; don't cache
            self._names[kind, game] = names
        return self._names[kind, game]

    async def get_suggestion(self, value: str) -> str | None:  # value is casefolded
        command, space, arg = value.partition(" ")
        if not space:
            candidates = self.local if value.startswith("/") else self.server if value.startswith("!") else []
            return next((c for c in candidates if c.casefold().startswith(value)), None)
        kind = {"!hint": "item", "!hint_location": "location"}.get(command)
        if not kind or not arg:
            return None
        name = next((n for n in self.names(kind) if n.casefold().startswith(arg)), None)
        return f"{command} {name}" if name else None


class CommandInput(Input):
    BINDINGS = [Binding("tab", "accept_suggestion", "Complete", show=False)]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.history: list[str] = []
        self.history_index = 0

    def action_accept_suggestion(self) -> None:
        self.action_cursor_right()  # at the end of the line this accepts the suggestion

    def remember(self, text: str) -> None:
        self.history.append(text)
        self.history_index = len(self.history)

    def on_key(self, event) -> None:
        if not self.history or event.key not in ("up", "down"):
            return
        if event.key == "up":
            self.history_index = max(0, self.history_index - 1)
        else:
            self.history_index = min(len(self.history), self.history_index + 1)
        self.value = self.history[self.history_index] if self.history_index < len(self.history) else ""
        self.cursor_position = len(self.value)
        event.stop()


class ClientApp(App):
    TITLE = "Archipelago"
    CSS = """
    #status { height: 1; padding: 0 1; background: $panel; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0; }
    RichLog, DataTable { height: 1fr; }
    """
    # alt+digit is unreliable (textual reads ESC 3 as the pound key), so ctrl letters the Input doesn't use
    BINDINGS = [("ctrl+q", "quit", "Quit"), ("ctrl+n", "switch_tab(1)", "Next tab"),
                ("ctrl+b", "switch_tab(-1)", "Prev tab")]

    def __init__(self, ctx: CommonContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.command_processor = ctx.command_processor(ctx)
        self.logger_tabs: dict[str, RichLog] = {}
        self._hints_snapshot: list | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with TabbedContent(id="tabs"):
            with TabPane("Archipelago", id="tab-archipelago"):
                yield RichLog(id="log-archipelago", wrap=True)
            with TabPane("All", id="tab-all"):
                yield RichLog(id="log-all", wrap=True)
            with TabPane("Hints", id="tab-hints"):
                yield DataTable(id="hints", cursor_type="row", zebra_stripes=True)
        yield CommandInput(placeholder="Command or chat. /help and !help list commands; Tab completes.",
                           suggester=CommandSuggester(self.ctx))
        yield Footer()

    def on_mount(self) -> None:
        self.title = type(self.ctx).__name__
        theme = prefs.get("theme")
        if theme in self.available_themes:
            self.theme = theme
        self.theme_changed_signal.subscribe(self, lambda theme: prefs.set("theme", theme.name))

        self.query_one("#hints", DataTable).add_columns("Receiving", "Item", "Finding", "Location", "Entrance",
                                                        "Status")
        for name, msg in log_buffer.records:
            self.route_log(name, msg)
        log_buffer.sink = self.write_log
        self.set_interval(0.5, self.refresh_state)
        self.refresh_state()
        self.query_one(CommandInput).focus()

    # logging

    def write_log(self, name: str, msg: str) -> None:
        # log records can come from other threads
        if self._thread_id == threading.get_ident():
            self.route_log(name, msg)
        else:
            self.call_from_thread(self.route_log, name, msg)

    def route_log(self, name: str, msg: str) -> None:
        text = Text.from_ansi(msg)
        self.query_one("#log-all", RichLog).write(text)
        if name in ARCHIPELAGO_LOGGERS:
            self.query_one("#log-archipelago", RichLog).write(text)
        elif name not in NO_TAB_LOGGERS:
            self.logger_tab(name).write(text)

    def logger_tab(self, name: str) -> RichLog:
        if name not in self.logger_tabs:
            log = RichLog(wrap=True)
            self.logger_tabs[name] = log
            pane_id = f"tab-logger-{len(self.logger_tabs)}"  # logger names aren't valid ids
            self.query_one(TabbedContent).add_pane(TabPane(name, log, id=pane_id), before="tab-hints")
        return self.logger_tabs[name]

    def echo(self, text: str) -> None:
        line = Text(text, style="orange1")
        self.query_one("#log-all", RichLog).write(line)
        self.query_one("#log-archipelago", RichLog).write(line)

    def action_switch_tab(self, step: int) -> None:
        tabs = self.query_one(TabbedContent)
        ids = [ContentTab.sans_prefix(tab.id) for tab in tabs.query(ContentTab)]  # tab bar order, not DOM order
        tabs.active = ids[(ids.index(tabs.active) + step) % len(ids)]

    # status bar and hints

    def refresh_state(self) -> None:
        ctx = self.ctx
        status = Text()
        if ctx.server and ctx.server.socket and not ctx.server.socket.closed:
            status.append("● ", style="green").append(f"Connected to {ctx.server_address}")
        else:
            status.append("○ ", style="red").append("Not connected (/connect address:port)")
        if ctx.slot:
            status.append(f"  │  {ctx.player_names.get(ctx.slot, ctx.auth)}")
        if ctx.game:
            status.append(f"  │  {ctx.game}")
        if ctx.slot and ctx.hint_points is not None:
            status.append(f"  │  Hint points: {ctx.hint_points}")
        self.query_one("#status", Static).update(status)
        self.refresh_hints()

    def refresh_hints(self) -> None:
        ctx = self.ctx
        hints = ctx.stored_data.get(f"_read_hints_{ctx.team}_{ctx.slot}", []) if ctx.slot else []
        if hints == self._hints_snapshot:
            return
        self._hints_snapshot = [dict(hint) for hint in hints]

        from NetUtils import HintStatus
        names = {HintStatus.HINT_FOUND: "Found", HintStatus.HINT_UNSPECIFIED: "Unspecified",
                 HintStatus.HINT_NO_PRIORITY: "No Priority", HintStatus.HINT_AVOID: "Avoid",
                 HintStatus.HINT_PRIORITY: "Priority"}
        colors = {HintStatus.HINT_FOUND: "green", HintStatus.HINT_UNSPECIFIED: "white",
                  HintStatus.HINT_NO_PRIORITY: "cyan", HintStatus.HINT_AVOID: "salmon",
                  HintStatus.HINT_PRIORITY: "plum"}
        weights = {HintStatus.HINT_FOUND: 0, HintStatus.HINT_UNSPECIFIED: 1, HintStatus.HINT_NO_PRIORITY: 2,
                   HintStatus.HINT_AVOID: 3, HintStatus.HINT_PRIORITY: 4}

        def render(node: dict) -> Text:
            return Text.from_ansi(ctx.jsontotextparser.handle_node(node))

        def status_of(hint: dict) -> int:
            if "status" in hint:
                return hint["status"]
            return HintStatus.HINT_FOUND if hint["found"] else HintStatus.HINT_UNSPECIFIED  # old servers

        table = self.query_one("#hints", DataTable)
        cursor, scroll = table.cursor_row, table.scroll_y
        table.clear()
        for hint in sorted(hints, key=lambda h: weights.get(status_of(h), 0), reverse=True):
            status = status_of(hint)
            status_text = Text(names.get(status, "Unknown"), style=colors.get(status, "red"))
            if status != HintStatus.HINT_FOUND and ctx.slot_concerns_self(hint["receiving_player"]):
                status_text.stylize("underline")
            table.add_row(
                render({"type": "player_id", "text": hint["receiving_player"]}),
                render({"type": "item_id", "text": hint["item"], "flags": hint["item_flags"],
                        "player": hint["receiving_player"]}),
                render({"type": "player_id", "text": hint["finding_player"]}),
                render({"type": "location_id", "text": hint["location"], "player": hint["finding_player"]}),
                Text(hint["entrance"], style="blue") if hint["entrance"] else Text("Vanilla"),
                status_text,
            )
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1), animate=False)
            table.scroll_y = scroll

    # input

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        self.query_one(CommandInput).remember(text)

        ctx = self.ctx
        if ctx.input_requests > 0:
            ctx.input_requests -= 1
            ctx.input_queue.put_nowait(text)
            return
        if text.startswith(("/", "!")):
            self.echo(text)
        try:
            self.command_processor(text)
        except Exception as e:
            logging.getLogger("Client").exception(e)


_running: set[int] = set()  # ids of contexts with an active app


async def console_loop(ctx: CommonContext) -> None:
    if id(ctx) in _running:  # fallback already started it, then the client called run_cli late
        return
    _running.add(id(ctx))
    install_log_handler()
    app = ClientApp(ctx)

    async def watch_exit() -> None:
        await ctx.exit_event.wait()
        app.exit()

    watcher = asyncio.create_task(watch_exit())
    try:
        await app.run_async()
    finally:
        watcher.cancel()
        log_buffer.sink = None
        logging.getLogger().removeHandler(log_buffer)
        logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))  # shutdown messages
        ctx.exit_event.set()
        _running.discard(id(ctx))


FALLBACK_DELAY = 1.0
"""Seconds a new context gets to start its own input loop before the fallback starts the app."""


def install_fallback() -> None:
    """Start the app for clients that never call run_cli/console_loop (e.g. civ6), so they still get input and quit."""
    from CommonClient import CommonContext
    original_init = CommonContext.__init__

    def __init__(self, *args, **kwargs) -> None:
        original_init(self, *args, **kwargs)
        try:
            asyncio.get_running_loop().create_task(_fallback(self), name="aplite fallback")
        except RuntimeError:
            pass  # context created outside an event loop; nothing to attach to

    CommonContext.__init__ = __init__


async def _fallback(ctx: CommonContext) -> None:
    await asyncio.sleep(FALLBACK_DELAY)
    if not ctx.input_task and id(ctx) not in _running and not ctx.exit_event.is_set():
        ctx.input_task = asyncio.create_task(console_loop(ctx), name="Input")
