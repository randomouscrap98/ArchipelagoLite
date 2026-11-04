"""Base for every aplite app. Keys they share: ctrl+q quits aplite, ctrl+l goes (back) to the launcher, ctrl+p is
Textual's command palette, labelled Options. The app just ends; __main__ reads `next_step` to decide what runs after
it. Also: the saved theme, and terminal log output held back while an app is on screen."""
from __future__ import annotations

import contextlib
import functools
import logging
import os
import signal
import sys
import threading
import typing

from textual.app import App, SystemCommand
from textual.binding import Binding

from aplite import glyphs, prefs  # glyphs patches Textual on import

next_step: typing.Literal["quit", "launcher"] | None = None
"""What the user asked for when the last app ended (None: it ended on its own)."""

TTY_CHECK = 1.0
"""Seconds between checks that the terminal is still there (see AppliteApp.check_terminal)."""

QUIT_STATUS = 86
"""Exit status of a child process (file picker, command-line script) whose user pressed ctrl+q: quit aplite."""

T = typing.TypeVar("T")


def quit_requested(status: object) -> bool:
    """Whether a child's exit status (or a SystemExit code) means its user pressed ctrl+q; if so, aplite quits too."""
    global next_step
    if status == QUIT_STATUS:
        next_step = "quit"
        return True
    return False


def terminal_handlers(root: logging.Logger) -> list[logging.Handler]:
    """Root handlers that print to the terminal (Utils.init_logging adds one); they'd scribble over an app."""
    return [h for h in root.handlers if type(h) is logging.StreamHandler and h.stream in (sys.stdout, sys.stderr)]


@contextlib.contextmanager
def quiet_terminal() -> typing.Iterator[None]:
    """Detach root log handlers that write to the terminal while an app is on screen."""
    root = logging.getLogger()
    streams = terminal_handlers(root)
    for handler in streams:
        root.removeHandler(handler)
    try:
        yield
    finally:
        for handler in streams:
            root.addHandler(handler)


def on_app_thread(app: App, func: typing.Callable[..., T], *args) -> T:
    """Call `func` on the app's thread (directly when already there) and return its result."""
    if threading.get_ident() == app._thread_id:
        return func(*args)
    return app.call_from_thread(func, *args)


def _stdin_is_tty() -> bool:
    try:
        return os.isatty(sys.__stdin__.fileno())
    except (AttributeError, ValueError, OSError):  # no or closed stdin (multiprocessing children close it)
        return False


class AppliteApp(App):
    BINDINGS = [Binding("ctrl+q", "leave('quit')", "Quit"), Binding("ctrl+l", "leave('launcher')", "Launcher"),
                Binding("ctrl+p", "command_palette", "Options", show=False)]  # the footer shows it at the right
    LAUNCHER_KEY = True
    """False hides ctrl+l: in the launcher itself, and in the file picker child (its caller isn't done)."""

    def run(self, *args, **kwargs):
        """App.run without terminal log output on top (clients use run_async and route logs to their TUI)."""
        with quiet_terminal():
            return super().run(*args, **kwargs)

    def on_mount(self) -> None:  # Textual calls each class's on_mount, so subclasses get this too
        prefs.bind_theme(self)
        self.animation_level = "none"  # Textual's own animations (tab underline, smooth scrolls) lagged behind
        if hasattr(signal, "SIGHUP") and _stdin_is_tty():
            self.set_interval(TTY_CHECK, self.check_terminal)

    def check_terminal(self) -> None:
        """A closed terminal that didn't kill us (SIGHUP ignored or never delivered) makes stdin read EOF forever,
        and Textual's input thread spins on it at 100% CPU: die the way the hangup should have made us."""
        if not _stdin_is_tty():
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGHUP)

    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        other = "simple" if glyphs.mode == "fancy" else "fancy"
        yield SystemCommand(f"Glyphs: switch to {other}",
                            "Fancy: block-character borders and scrollbars. Simple: plain lines, for consoles "
                            "without block characters (conhost, Linux tty)", lambda: glyphs.set_mode(self, other))

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """While another screen is on top (file picker, save dialog), the app's own keys would act on the hidden
        screen below: hide them. Shared keys and Textual's own (Tab focus etc.) stay."""
        if parameters == ("launcher",) and not self.LAUNCHER_KEY:
            return False
        return len(self.screen_stack) == 1 or action not in _own_actions(type(self))

    def can_leave(self, step: str) -> bool:
        """Called on every ctrl+q / ctrl+l: the topmost screen with a can_leave (editors: unsaved work) decides."""
        for screen in reversed(self.screen_stack):
            guard = getattr(screen, "can_leave", None)
            if guard:
                return guard(step)
        return True

    def action_leave(self, step: typing.Literal["quit", "launcher"]) -> None:
        global next_step
        if self.can_leave(step):
            next_step = step
            self.exit()

    async def action_quit(self) -> None:  # the command palette's "Quit"
        self.action_leave("quit")


@functools.cache
def _own_actions(cls: type) -> frozenset[str]:
    """Actions bound by an AppliteApp subclass's own BINDINGS (not AppliteApp's shared ones, not Textual's)."""
    actions = set()
    for klass in cls.__mro__:
        if klass is AppliteApp:
            break
        for binding in klass.__dict__.get("BINDINGS", ()):
            action = binding.action if isinstance(binding, Binding) else binding[1]
            actions.add(action.split("(")[0])
    return frozenset(actions)
