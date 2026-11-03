"""
Terminal replacements for Utils' file dialogs (open_filename / open_directory), which otherwise need kdialog, zenity or
tkinter. Used for missing ROMs and game folders (settings.py), Install APWorld, Host without a file, etc.
Shows the full-screen file picker (a child process, see pick_file.py), falling back to a plain prompt. While a
client's TUI is on screen, it is suspended meanwhile and restored after.
"""
from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import typing

import Utils

active_app = None
"""The Textual app currently on screen that a prompt has to step around; set via around()."""
_cancelled: set[tuple[str, bool]] = set()
"""Prompts the user cancelled this run; not asked again (e.g. SNI asks for its folder on every reconnect)."""


@contextlib.contextmanager
def around(app) -> typing.Iterator[None]:
    """While `app` is on screen, prompts suspend it to ask in the terminal."""
    global active_app
    previous, active_app = active_app, app
    try:
        yield
    finally:
        active_app = previous


def install() -> None:
    """Must run before worlds load: LauncherComponents does `from Utils import open_filename`."""
    Utils.open_filename = open_filename
    Utils.open_directory = open_directory


def open_filename(title: str, filetypes: typing.Iterable[tuple[str, typing.Iterable[str]]],
                  suggest: str = "") -> str | None:
    suffixes = tuple(suffix for _, group in filetypes for suffix in group)
    return ask(title, folder=False, suffixes=suffixes, suggest=suggest)


def open_directory(title: str, suggest: str = "") -> str | None:
    return ask(title, folder=True, suffixes=(), suggest=suggest)


QUIT = "\0quit"
"""_ask's answer when the user pressed ctrl+q in the picker."""


def ask(title: str, folder: bool, suffixes: tuple[str, ...], suggest: str) -> str | None:
    if (title, folder) in _cancelled:
        return None
    answer = _ask(title, folder, suffixes, suggest)
    if answer == QUIT:
        _quit_aplite()
        return None
    if answer is None:
        _cancelled.add((title, folder))
        logging.info(f"{title}: cancelled; won't ask again until aplite restarts.")
    return answer


def _ask(title: str, folder: bool, suffixes: tuple[str, ...], suggest: str) -> str | None:
    app = active_app
    if app is None:
        return _pick_or_prompt(title, folder, suffixes, suggest)

    def suspended() -> str | None:
        try:
            with app.suspend():
                return _pick_or_prompt(title, folder, suffixes, suggest)
        except Exception as e:  # e.g. a driver that can't suspend
            logging.error(f"Couldn't ask for {title} in the terminal: {e}")
            return None

    from aplite import nav
    return nav.on_app_thread(app, suspended)


def _quit_aplite() -> None:
    """Ctrl+Q in the picker: end the app on screen (the client then shuts down) or, before/without one, this process.
    nav.QUIT_STATUS tells a parent aplite process (this one being a command-line script) to quit too."""
    from aplite import nav
    app = active_app
    if app is None:
        raise SystemExit(nav.QUIT_STATUS)
    nav.on_app_thread(app, app.exit)


def _pick_or_prompt(title: str, folder: bool, suffixes: tuple[str, ...], suggest: str) -> str | None:
    """The full-screen file picker (aplite/pick_file.py) in a child process; a plain terminal prompt if it fails."""
    import aplite
    from aplite import nav  # here, not at the top: aplite.script children shouldn't load Textual at startup
    fd, out = tempfile.mkstemp(prefix="aplite-pick-", suffix=".txt")
    os.close(fd)
    try:
        status = subprocess.call([sys.executable, "-m", "aplite.pick_file", out, title,
                                  "folder" if folder else "file", suggest or "", *suffixes],
                                 env=aplite.child_env())
        with open(out, encoding="utf-8") as f:
            answer = f.read().strip()
        if nav.quit_requested(status):
            return QUIT
        if status == 0:
            return answer or None
        logging.warning(f"File picker failed (exit {status}); asking in the terminal instead.")
    except OSError as e:
        logging.warning(f"File picker failed ({e}); asking in the terminal instead.")
    finally:
        with contextlib.suppress(OSError):
            os.remove(out)
    return _prompt(title, folder, suffixes, suggest)


def _prompt(title: str, folder: bool, suffixes: tuple[str, ...], suggest: str) -> str | None:
    kind = "folder" if folder else "file"
    print(f"\n{title}")
    if suffixes:
        print(f"  expected: {', '.join(suffixes)}")
    if suggest:
        print(f"  suggested: {suggest}")
    print(f"  Type a path to the {kind} (Tab completes), or leave empty (or Ctrl+C) to cancel.")
    with _path_completion(folder), _ctrl_c_cancels():
        while True:
            try:
                answer = input("> ").strip()
            except EOFError:
                return None
            except KeyboardInterrupt:
                print("\nCancelled.")
                return None
            if not answer:
                print("Cancelled.")
                return None
            path = os.path.abspath(os.path.expanduser(answer))
            if folder and os.path.isdir(path) or not folder and os.path.isfile(path):
                if suffixes and not folder and not path.lower().endswith(tuple(s.lower() for s in suffixes)):
                    print(f"  Note: expected {', '.join(suffixes)}; using it anyway.")
                return path
            print(f"  Not a {kind}: {path}")


@contextlib.contextmanager
def _ctrl_c_cancels() -> typing.Iterator[None]:
    """Ctrl+C raises KeyboardInterrupt in input() for the prompt to catch. Otherwise asyncio.run's handler (clients)
    would cancel the whole client while the prompt carries on, and crash it once the prompt returns."""
    if threading.current_thread() is not threading.main_thread():
        yield  # only the main thread can change signal handlers (and receives the signal)
        return
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


class _path_completion:
    """Tab completion of paths for input(), via readline when available (not on Windows)."""

    def __init__(self, folders_only: bool) -> None:
        self.folders_only = folders_only

    def complete(self, text: str, state: int) -> str | None:
        expanded = os.path.expanduser(text)
        folder, prefix = os.path.split(expanded)
        try:
            names = sorted(entry.name for entry in os.scandir(folder or ".") if entry.name.startswith(prefix))
        except OSError:
            names = []
        options = []
        for name in names:
            full = os.path.join(folder, name)
            if os.path.isdir(full):
                options.append(full + os.sep)
            elif not self.folders_only:
                options.append(full)
        return options[state] if state < len(options) else None

    def __enter__(self) -> "_path_completion":
        try:
            import readline
        except ImportError:
            self.readline = None
            return self
        self.readline = readline
        self.old = (readline.get_completer(), readline.get_completer_delims())
        readline.set_completer(self.complete)
        readline.set_completer_delims("\t\n")
        # uv's Pythons use libedit, which has its own binding syntax
        if getattr(readline, "backend", "") == "editline" or "libedit" in (readline.__doc__ or ""):
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")
        return self

    def __exit__(self, *exc) -> None:
        if self.readline:
            self.readline.set_completer(self.old[0])
            self.readline.set_completer_delims(self.old[1])
