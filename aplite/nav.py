"""Base for every aplite app. Keys they share: ctrl+q quits aplite, ctrl+l goes (back) to the launcher, ctrl+p is
Textual's command palette, labelled Options. The app just ends; __main__ reads `next_step` to decide what runs after
it. Also: the saved theme, and terminal log output held back while an app is on screen."""
from __future__ import annotations

import contextlib
import dataclasses
import functools
import logging
import os
import signal
import sys
import threading
import time
import typing

from textual.app import App, SystemCommand
from textual.binding import Binding
from textual.widgets import OptionList
from textual.theme import BUILTIN_THEMES, Theme

import aplite
from aplite import colors, glyphs, prefs  # glyphs patches Textual on import

next_step: typing.Literal["quit", "launcher"] | None = None
"""What the user asked for when the last app ended (None: it ended on its own)."""

TTY_CHECK = 1.0
"""Seconds between checks that the terminal is still there (see AppliteApp.check_terminal)."""

T = typing.TypeVar("T")

CONFIRM_SECONDS = 6
"""How long a "press again to confirm" warning stays armed."""


class Confirm:
    """"Press again to confirm" for risky actions: the first call for `key` warns and returns False; a second call for
    the same key within CONFIRM_SECONDS returns True. A call for another key re-arms for that one."""
    def __init__(self) -> None:
        self.key: typing.Hashable = None
        self.armed_at = 0.0

    def __call__(self, widget: typing.Any, key: typing.Hashable, message: str) -> bool:
        if key == self.key and time.monotonic() - self.armed_at < CONFIRM_SECONDS:
            self.reset()
            return True
        self.key, self.armed_at = key, time.monotonic()
        widget.notify(message, severity="warning", timeout=CONFIRM_SECONDS)
        return False

    def reset(self) -> None:
        self.key = None


def quit_if_requested(status: object) -> bool:
    """If a child's exit status (or a SystemExit code) says its user pressed ctrl+q, set next_step to "quit" (aplite
    quits too) and return True."""
    global next_step
    if status == aplite.QUIT_STATUS:
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


HIGH_CONTRAST = Theme(
    name="high-contrast", dark=True,  # dark: the client uses the dark role colors
    primary="#FFFF00", secondary="#00FFFF", accent="#00FFFF", warning="#FFFF00", error="#FF5555", success="#00FF00",
    foreground="#FFFFFF", background="#000000", surface="#000000", panel="#000000", text_alpha=1.0,
    variables={
        "text": "#FFFFFF", "text-muted": "#C0C0C0", "text-disabled": "#A0A0A0",
        "border": "#FFFF00", "border-blurred": "#808080",
        "block-cursor-foreground": "#000000", "block-cursor-background": "#FFFF00",
        "block-cursor-blurred-foreground": "#000000", "block-cursor-blurred-background": "#00FFFF",
        "block-hover-background": "#303030",
        "input-cursor-foreground": "#000000", "input-cursor-background": "#FFFFFF",
        "input-selection-foreground": "#000000", "input-selection-background": "#00FFFF",
        "footer-background": "#000000", "footer-key-foreground": "#FFFF00", "footer-description-foreground": "#FFFFFF",
        "scrollbar": "#808080", "scrollbar-hover": "#C0C0C0", "scrollbar-active": "#FFFF00",
        "scrollbar-background": "#000000", "scrollbar-background-hover": "#000000",
        "scrollbar-background-active": "#000000",
        "aplite-outline": "#808080", "aplite-dim": "#C0C0C0",
    })
"""Black and white with yellow focus and cyan unfocused highlights; pure colors, so 16-color terminals match them.
aplite-outline: unfocused box outlines (glyphs), aplite-dim: replaces dim text (colors.RoleFilter)."""


# Textual's unfocused-highlight rule ignores $block-cursor-blurred-foreground (its default: the foreground, which this
# rule used anyway); high contrast needs black there, on cyan
_HIGHLIGHT = "color: $foreground;\n            background: $block-cursor-blurred-background;"
OptionList.DEFAULT_CSS = OptionList.DEFAULT_CSS.replace(
    _HIGHLIGHT, _HIGHLIGHT.replace("$foreground", "$block-cursor-blurred-foreground"))


def register_themes(app: App) -> None:
    """High contrast, and Textual's ANSI themes adjusted: they draw an unfocused list's highlight in the terminal's
    default colors (invisible, and our filter-field lists (launcher, editors) are never focused): blue / cyan instead
    (reverse would suit any palette, but Textual 8.2 drops it from list highlights; gray is the background in some
    palettes, e.g. Solarized). Their outlines: gray (glyphs' panel-colored ones would be the default text color)."""
    app.register_theme(HIGH_CONTRAST)
    for name, extra in (("ansi-dark", {"border": "ansi_cyan", "block-cursor-blurred-background": "ansi_blue",
                                       "block-cursor-blurred-foreground": "ansi_bright_white",
                                       "aplite-outline": "ansi_bright_black"}),
                        ("ansi-light", {"block-cursor-blurred-background": "ansi_cyan",  # dark text on blue: unreadable
                                        "aplite-outline": "ansi_white"})):
        theme = BUILTIN_THEMES[name]
        app.register_theme(dataclasses.replace(theme, variables={**theme.variables, **extra}))


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
        register_themes(self)
        prefs.bind_theme(self)
        colors.install(self)
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
        yield SystemCommand("Client Colors", "Items, players, locations, hint statuses", self.open_client_colors)
        from aplite import background
        if background.SUPPORTED:
            yield SystemCommand(f"Background spares: {background.spare_count() or 'off'}",
                                "Keep aplite loaded in the background so it opens instantly", self.open_background)

    def open_background(self) -> None:
        from aplite.background_ui import BackgroundModal
        if not isinstance(self.screen, BackgroundModal):
            self.push_screen(BackgroundModal())

    def open_client_colors(self) -> None:
        from aplite.client_colors import ClientColorsScreen
        if not isinstance(self.screen, ClientColorsScreen):
            self.push_screen(ClientColorsScreen())

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
