"""
Terminal replacements for Utils' file dialogs (open_filename / open_directory), which otherwise need kdialog, zenity or
tkinter. Used for missing ROMs and game folders (settings.py), Install APWorld, Host without a file, etc.
While a client's TUI is on screen, it is suspended for the prompt and restored after.
"""
from __future__ import annotations

import contextlib
import logging
import os
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


def ask(title: str, folder: bool, suffixes: tuple[str, ...], suggest: str) -> str | None:
    if (title, folder) in _cancelled:
        return None
    answer = _ask(title, folder, suffixes, suggest)
    if answer is None:
        _cancelled.add((title, folder))
        logging.info(f"{title}: cancelled; won't ask again until aplite restarts.")
    return answer


def _ask(title: str, folder: bool, suffixes: tuple[str, ...], suggest: str) -> str | None:
    app = active_app
    if app is None:
        return _prompt(title, folder, suffixes, suggest)

    def suspended() -> str | None:
        try:
            with app.suspend():
                return _prompt(title, folder, suffixes, suggest)
        except Exception as e:  # e.g. a driver that can't suspend
            logging.error(f"Couldn't ask for {title} in the terminal: {e}")
            return None

    if threading.get_ident() == app._thread_id:
        return suspended()
    return app.call_from_thread(suspended)


def _prompt(title: str, folder: bool, suffixes: tuple[str, ...], suggest: str) -> str | None:
    kind = "folder" if folder else "file"
    print(f"\n{title}")
    if suffixes:
        print(f"  expected: {', '.join(suffixes)}")
    if suggest:
        print(f"  suggested: {suggest}")
    print(f"  Type a path to the {kind} (Tab completes), or leave empty to cancel.")
    with _path_completion(folder):
        while True:
            try:
                answer = input("> ").strip()
            except EOFError:
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
