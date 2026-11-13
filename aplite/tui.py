"""Textual replacement for CommonClient.console_loop. ctx.ui stays None so kivy-only client code is skipped."""
from __future__ import annotations

import asyncio
import functools
import logging
import sys
import typing

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets.input import Selection
from textual.widgets.option_list import Option
from textual.widgets.tabbed_content import ContentTab
from textual.widgets import Button, DataTable, Footer, Input, OptionList, RichLog, Static, TabbedContent, TabPane

from aplite import colors, glyphs, prompts, tracker
from aplite.nav import AppliteApp, on_app_thread, terminal_handlers

if typing.TYPE_CHECKING:
    from CommonClient import CommonContext

STATE_REFRESH = 0.5
"""Seconds between status bar / hints checks (upstream tells the kivy UI through ctx.ui, which aplite leaves None)."""
RELEASE_TIMEOUT = 5.0
"""Seconds to keep answering a quitting client's input requests before leaving it to upstream's shutdown."""
FALLBACK_DELAY = 1.0
"""Seconds a new context gets to start its own input loop before the fallback starts the app."""


@functools.cache
def hint_status_display() -> tuple[dict[int, str], dict[int, str], dict[int, int]]:
    """(names, color roles, sort order; higher first) per hint status. Names and order copied from kvui.py
    (status_names, status_sort_weights), since importing kvui loads kivy: keep in sync when upstream changes them.
    Built on first use because importing NetUtils takes ~150ms, which the launcher shouldn't pay."""
    from NetUtils import HintStatus
    names = {HintStatus.HINT_FOUND: "Found", HintStatus.HINT_UNSPECIFIED: "Unspecified",
             HintStatus.HINT_NO_PRIORITY: "No Priority", HintStatus.HINT_AVOID: "Avoid",
             HintStatus.HINT_PRIORITY: "Priority"}
    order = {HintStatus.HINT_FOUND: 0, HintStatus.HINT_UNSPECIFIED: 1, HintStatus.HINT_NO_PRIORITY: 2,
             HintStatus.HINT_AVOID: 3, HintStatus.HINT_PRIORITY: 4}
    return names, colors.hint_roles(), order


def read_hints(ctx) -> list[dict]:
    """This slot's hints, as the server keeps them in data storage (the client asks for this key on connect)."""
    return ctx.stored_data.get(f"_read_hints_{ctx.team}_{ctx.slot}", []) if ctx.slot else []


ARCHIPELAGO_LOGGERS = {"Client", "StreamLog"}
"""Top-level logger names shown in the Archipelago tab (StreamLog carries server messages)."""
NO_TAB_LOGGERS = ARCHIPELAGO_LOGGERS | {"root", "FileLog", "websockets", "asyncio", "textual", "performance",
                                        "Benchmark", "Worlds"}
"""Top-level logger names that never get their own tab; anything else that logs gets one (e.g. FactorioServer)."""


class LogBuffer(logging.Handler):
    """Root log handler owned by this module: keeps history so records logged before the client TUI mounts can be
    replayed, and forwards new ones to the TUI (sink) while it runs."""

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

    def attach(self, replace_terminal: bool = False) -> None:
        """Start collecting. Safe to call again (Utils.init_logging wipes root handlers). With `replace_terminal`,
        records stop going to the terminal too (the TUI shows them instead)."""
        root = logging.getLogger()
        if replace_terminal:
            for handler in terminal_handlers(root):
                root.removeHandler(handler)
        if self not in root.handlers:
            root.addHandler(self)

    def detach(self) -> None:
        """Stop forwarding; later records (e.g. shutdown messages) go to the terminal again."""
        self.sink = None
        root = logging.getLogger()
        root.removeHandler(self)
        if not terminal_handlers(root):
            root.addHandler(logging.StreamHandler(sys.stdout))


log_buffer = LogBuffer()


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


class HintStatusModal(ModalScreen[int | None]):
    """Pick a hint's priority; the statuses kvui's dropdown offers (the server refuses Found)."""
    CSS = """
    HintStatusModal { align: center middle; }
    HintStatusModal Vertical { width: 30; height: auto; border: thick $primary; background: $surface; }
    HintStatusModal Static { padding: 0 1; }
    HintStatusModal OptionList { height: auto; border: none; }
    """
    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def __init__(self, current: int) -> None:
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        from NetUtils import HintStatus
        names, roles, _ = hint_status_display()
        self.statuses = [HintStatus.HINT_NO_PRIORITY, HintStatus.HINT_PRIORITY, HintStatus.HINT_AVOID]
        with Vertical():
            yield Static("[b]Hint priority[/b]")
            yield OptionList(*[Option(Text(names[s], style=colors.style(roles[s]))) for s in self.statuses])

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        options.highlighted = self.statuses.index(self.current) if self.current in self.statuses else 0
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(self.statuses[event.option_index])


class ClientApp(AppliteApp):
    TITLE = "Archipelago"
    CSS = """
    #connect-bar { height: 1; background: $panel; }
    #address { width: 1fr; max-width: 50; }
    #connect { min-width: 14; }
    #status { width: 1fr; padding: 0 1; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0; }
    RichLog, DataTable { height: 1fr; }
    """
    # alt+digit is unreliable (textual reads ESC 3 as the pound key), ctrl+pageup/pagedown are terminal tab keys (GNOME
    # Terminal, Konsole...): Ctrl+N / Ctrl+B switch tabs on every aplite screen with tabs
    BINDINGS = [("ctrl+n", "switch_tab(1)", "Next tab"), ("ctrl+b", "switch_tab(-1)", "Prev tab"),
                ("ctrl+t", "focus_address", "Address"), Binding("ctrl+f", "focus_search", "Search")]

    def __init__(self, ctx: CommonContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.command_processor = ctx.command_processor(ctx)
        self.logger_tabs: dict[str, RichLog] = {}
        self._hints_snapshot: list | None = None
        self._hint_rows: list[tuple[dict, int, bool]] = []
        """(hint, status, editable) per hints table row; row keys index this."""

    def compose(self) -> ComposeResult:
        with Horizontal(id="connect-bar"):
            yield Input(self.ctx.suggested_address or "archipelago.gg:", id="address", compact=True,
                        placeholder="address:port")
            yield Button("Connect", id="connect", compact=True)
            yield Static(id="status")
        with TabbedContent(id="tabs"):
            with TabPane("Archipelago", id="tab-archipelago"):
                yield RichLog(id="log-archipelago", wrap=True)
            with TabPane("All", id="tab-all"):
                yield RichLog(id="log-all", wrap=True)
            for title, widget in getattr(self.ctx, "aplite_tabs", list)():  # a client's own tabs (aplite.manual)
                with TabPane(title, id=f"tab-{title.lower()}"):
                    yield widget
            if tracker.supported(self.ctx):
                with TabPane("Tracker", id="tab-tracker"):
                    yield tracker.TrackerPane(self.ctx)
            with TabPane("Hints", id="tab-hints"):
                yield DataTable(id="hints", cursor_type="row", zebra_stripes=True)
        yield CommandInput(placeholder="Command or chat. /help and !help list commands; Tab completes.",
                           suggester=CommandSuggester(self.ctx))
        yield Footer()

    def on_mount(self) -> None:
        self.title = type(self.ctx).__name__
        self.query_one("#hints", DataTable).add_columns("Receiving", "Item", "Finding", "Location", "Entrance",
                                                        "Status")
        for name, msg in log_buffer.records:
            self.route_log(name, msg)
        log_buffer.sink = self.write_log
        self.set_interval(STATE_REFRESH, self.refresh_state)
        self.refresh_state()
        self.query_one(CommandInput).focus()

    # logging

    def write_log(self, name: str, msg: str) -> None:
        on_app_thread(self, self.route_log, name, msg)  # log records can come from other threads

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
        line = Text(text, style=colors.style("echo"))
        self.query_one("#log-all", RichLog).write(line)
        self.query_one("#log-archipelago", RichLog).write(line)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "focus_search" and not self.query(".tab-search"):
            return False
        return super().check_action(action, parameters)

    def action_focus_command(self) -> None:
        self.query_one(CommandInput).focus()

    def action_focus_search(self) -> None:
        """The search (`.tab-search`) of the current tab when it has one (Tracker, Manual), else the first tab's."""
        tabs = self.query_one(TabbedContent)
        search = tabs.query_one(f"#{tabs.active}").query(".tab-search")
        if search:
            search.first().focus()
        elif search := self.query(".tab-search"):
            tabs.active = search.first().query_ancestor(TabPane).id
            search.first().focus()

    def action_switch_tab(self, step: int) -> None:
        tabs = self.query_one(TabbedContent)
        ids = [ContentTab.sans_prefix(tab.id) for tab in tabs.query(ContentTab)]  # tab bar order, not DOM order
        tabs.active = ids[(ids.index(tabs.active) + step) % len(ids)]

    # status bar and hints

    def refresh_state(self) -> None:
        ctx = self.ctx
        status = Text()
        connected = bool(ctx.server and ctx.server.socket and not ctx.server.socket.closed)
        if connected:
            status.append(f"{glyphs.symbol('on')} ", style=colors.style("connected")).append("Connected")
        else:
            status.append(f"{glyphs.symbol('off')} ", style=colors.style("not_connected")).append("Not connected")
        self.refresh_connect_bar(connected)
        if ctx.slot:
            status.append(f"  │  {ctx.player_names.get(ctx.slot, ctx.auth)}")
        if ctx.game:
            status.append(f"  │  {ctx.game}")
        if ctx.slot and ctx.hint_points is not None:
            status.append(f"  │  Hint points: {ctx.hint_points}")
        self.query_one("#status", Static).update(status)
        self.refresh_hints()
        for pane in self.query(tracker.TrackerPane):
            pane.refresh_lines()

    def refresh_hints(self) -> None:
        ctx = self.ctx
        hints = read_hints(ctx)
        if hints == self._hints_snapshot:
            return
        self._hints_snapshot = [dict(hint) for hint in hints]
        from NetUtils import HintStatus
        names, roles, order = hint_status_display()

        def render(node: dict) -> Text:
            return Text.from_ansi(ctx.jsontotextparser.handle_node(node))

        def status_of(hint: dict) -> int:
            if "status" in hint:
                return hint["status"]
            return HintStatus.HINT_FOUND if hint["found"] else HintStatus.HINT_UNSPECIFIED  # old servers

        table = self.query_one("#hints", DataTable)
        cursor, scroll = table.cursor_row, table.scroll_y
        table.clear()
        self._hint_rows = []
        for hint in sorted(hints, key=lambda h: order.get(status_of(h), 0), reverse=True):
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
            self._hint_rows.append((hint, status, editable))
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1), animate=False)
            table.scroll_y = scroll

    @on(DataTable.RowSelected, "#hints")
    def hint_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a hint for one of this slot's unfound items picks its priority (kvui's status dropdown)."""
        hint, status, editable = self._hint_rows[int(event.row_key.value)]
        if not editable:
            self.notify("Only hints for your own unfound items can be changed.", severity="warning")
            return

        def chosen(new_status: int | None) -> None:
            if new_status is not None and new_status != status:
                self.ctx.update_hint(hint["location"], hint["finding_player"], new_status)

        self.push_screen(HintStatusModal(status), chosen)

    # connect bar (kvui's server bar: Enter or the button connects, the button disconnects)

    def refresh_connect_bar(self, connected: bool) -> None:
        address = self.query_one("#address", Input)
        self.query_one("#connect", Button).label = "Disconnect" if connected else "Connect"
        if connected and self.ctx.server_address:
            address.value = self.ctx.server_address.replace("ws://", "").replace("wss://", "")
        address.disabled = connected

    def action_focus_address(self) -> None:
        address = self.query_one("#address", Input)
        if address.disabled:
            return
        address.focus()
        s = address.value  # select the port if there is one, else the host (as kvui does)
        host_start = s.find("@") + 1
        ipv6_end = s.find("]", host_start) + 1
        port_start = s.find(":", ipv6_end if ipv6_end > 0 else host_start) + 1
        address.selection = Selection(port_start if port_start > 0 else host_start, len(s))

    @on(Input.Submitted, "#address")
    @on(Button.Pressed, "#connect")
    def connect_pressed(self, event: Input.Submitted | Button.Pressed) -> None:
        from CommonClient import async_start
        ctx = self.ctx
        ctx.username = None
        ctx.password = None
        if ctx.server:
            async_start(ctx.disconnect(), name="disconnecting")
        elif address := self.query_one("#address", Input).value.strip().removeprefix("/connect "):
            async_start(ctx.connect(address), name="connecting")
        self.query_one(CommandInput).focus()

    # input

    @on(Input.Submitted, "CommandInput")
    def command_submitted(self, event: Input.Submitted) -> None:
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


RUNNING = "_aplite_tui"
"""Marker attribute on a CommonContext whose TUI is running."""


async def console_loop(ctx: CommonContext) -> None:
    if getattr(ctx, RUNNING, False):  # fallback already started it, then the client called run_cli late
        return
    setattr(ctx, RUNNING, True)
    log_buffer.attach(replace_terminal=True)
    upstream_parser = ctx.jsontotextparser
    ctx.jsontotextparser = colors.make_parser(ctx)  # role markers; only RoleFilter in a running app reads them
    app = ClientApp(ctx)

    async def watch_exit() -> None:
        await ctx.exit_event.wait()
        app.exit()

    watcher = asyncio.create_task(watch_exit())
    try:
        with prompts.around(app):  # file prompts (missing ROMs etc.) suspend the TUI while asking
            await app.run_async()
    finally:
        watcher.cancel()
        ctx.jsontotextparser = upstream_parser
        log_buffer.detach()
        await _release_input_requests(ctx)
        ctx.exit_event.set()
        setattr(ctx, RUNNING, False)


async def _release_input_requests(ctx: CommonContext) -> None:
    """Upstream's shutdown awaits the server task before answering pending input requests, so quitting while the
    client asks for a slot name or password hung forever. Close the connection first (so an answer isn't sent as a
    login), then answer with None until the server task ends: it may ask again (password, then slot name)."""
    if not ctx.input_requests:
        return
    if ctx.server and not ctx.server.socket.closed:
        await ctx.server.socket.close()
    deadline = asyncio.get_running_loop().time() + RELEASE_TIMEOUT
    while ctx.server_task and not ctx.server_task.done() and asyncio.get_running_loop().time() < deadline:
        while ctx.input_requests > 0:
            ctx.input_queue.put_nowait(None)
            ctx.input_requests -= 1
        await asyncio.sleep(0.05)


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
    if not ctx.input_task and not getattr(ctx, RUNNING, False) and not ctx.exit_event.is_set():
        ctx.input_task = asyncio.create_task(console_loop(ctx), name="Input")
