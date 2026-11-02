"""Textual replacement for CommonClient.console_loop. ctx.ui stays None so kivy-only client code is skipped."""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import typing

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Footer, Input, RichLog

if typing.TYPE_CHECKING:
    from CommonClient import CommonContext


class LogBuffer(logging.Handler):
    """Root log handler; keeps history so records logged before the app mounts can be replayed."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []
        self.sink: typing.Callable[[str], None] | None = None

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "NoStream", False):  # same filter as Utils.init_logging's stream handler
            return
        try:
            msg = self.format(record)
        except Exception:
            self.handleError(record)
            return
        self.records.append(msg)
        if self.sink:
            self.sink(msg)


log_buffer = LogBuffer()


def install_log_handler() -> None:
    """Route root logging to the TUI instead of the terminal. Safe to call repeatedly (init_logging wipes handlers)."""
    root = logging.getLogger()
    for handler in root.handlers[:]:
        if type(handler) is logging.StreamHandler and handler.stream in (sys.stdout, sys.stderr):
            root.removeHandler(handler)
    if log_buffer not in root.handlers:
        root.addHandler(log_buffer)


class ClientApp(App):
    TITLE = "Archipelago"
    CSS = """
    RichLog { height: 1fr; }
    """
    BINDINGS = [("ctrl+q", "quit", "Quit")]

    def __init__(self, ctx: CommonContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.command_processor = ctx.command_processor(ctx)
        self.history: list[str] = []
        self.history_index = 0

    def compose(self) -> ComposeResult:
        yield RichLog(wrap=True, markup=False, highlight=False)
        yield Input(placeholder="Command or chat. /help for commands.")
        yield Footer()

    def on_mount(self) -> None:
        self.title = type(self.ctx).__name__
        log = self.query_one(RichLog)
        for msg in log_buffer.records:
            log.write(Text.from_ansi(msg))
        log_buffer.sink = self.write_log
        self.query_one(Input).focus()

    def write_log(self, msg: str) -> None:
        # log records can come from other threads
        if self._thread_id == threading.get_ident():
            self.query_one(RichLog).write(Text.from_ansi(msg))
        else:
            self.call_from_thread(self.query_one(RichLog).write, Text.from_ansi(msg))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        self.history.append(text)
        self.history_index = len(self.history)

        ctx = self.ctx
        if ctx.input_requests > 0:
            ctx.input_requests -= 1
            ctx.input_queue.put_nowait(text)
            return
        if text.startswith(("/", "!")):
            self.query_one(RichLog).write(Text(text, style="orange1"))
        try:
            self.command_processor(text)
        except Exception as e:
            logging.getLogger("Client").exception(e)

    def on_key(self, event) -> None:
        if not self.history or not isinstance(self.focused, Input):
            return
        if event.key == "up":
            self.history_index = max(0, self.history_index - 1)
        elif event.key == "down":
            self.history_index = min(len(self.history), self.history_index + 1)
        else:
            return
        inp = self.query_one(Input)
        inp.value = self.history[self.history_index] if self.history_index < len(self.history) else ""
        inp.cursor_position = len(inp.value)
        event.stop()


async def console_loop(ctx: CommonContext) -> None:
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
