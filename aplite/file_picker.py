"""File picker screen: dismisses with the chosen path, or None to go back. Remembers the folder in a pref."""
from __future__ import annotations

import os
import typing
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DirectoryTree, Footer, Input, Static

from aplite import prefs


class FilteredTree(DirectoryTree):
    BINDINGS = [Binding("backspace", "parent", "Parent folder")]

    def __init__(self, path: Path, suffixes: typing.Sequence[str]) -> None:
        super().__init__(path)
        self.suffixes = tuple(suffixes)
        self.show_all = False

    def filter_paths(self, paths: typing.Iterable[Path]) -> typing.Iterable[Path]:
        return [p for p in paths if not p.name.startswith(".") and
                (p.is_dir() or self.show_all or p.name.endswith(self.suffixes))]

    def action_parent(self) -> None:
        self.path = Path(self.path).resolve().parent
        self.screen.update_folder()


class FilePicker(Screen[Path | None]):
    CSS = """
    #folder { height: 1; padding: 0 1; background: $panel; }
    FilteredTree { height: 1fr; }
    """
    BINDINGS = [Binding("escape", "back", "Back"), Binding("ctrl+t", "toggle_all", "Show all files")]

    def __init__(self, title: str, suffixes: typing.Sequence[str], pref_key: str, default_dir: str) -> None:
        """Starts in the folder saved under `pref_key`, else `default_dir`; only shows `suffixes` until toggled."""
        super().__init__()
        self.title_text = title
        self.suffixes = suffixes
        self.pref_key = pref_key
        last = prefs.get(pref_key)
        self.start = Path(last if last and os.path.isdir(last) else default_dir)

    def compose(self) -> ComposeResult:
        yield Static(id="folder")
        yield FilteredTree(self.start, self.suffixes)
        yield Input(placeholder="Path to a file or folder. Enter opens; Tab switches between list and path.")
        yield Footer()

    def on_mount(self) -> None:
        self.update_folder()
        self.query_one(FilteredTree).focus()

    def update_folder(self) -> None:
        tree = self.query_one(FilteredTree)
        shown = "all files" if tree.show_all else ", ".join(self.suffixes)
        self.query_one("#folder", Static).update(f"{self.title_text}: {Path(tree.path).resolve()}  ({shown})")

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        event.stop()
        self.choose(event.path)

    def on_tree_node_highlighted(self, event) -> None:
        entry = event.node.data
        if entry is not None:
            self.query_one(Input).value = str(entry.path)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()  # the app below may handle Input.Submitted too
        path = Path(os.path.expanduser(event.value.strip()))
        tree = self.query_one(FilteredTree)
        if path.is_dir():
            tree.path = path
            self.update_folder()
            tree.focus()
        elif path.is_file():
            self.choose(path)
        else:
            self.notify(f"Not found: {path}", severity="error")

    def choose(self, path: Path) -> None:
        prefs.set(self.pref_key, str(path.resolve().parent))
        self.dismiss(path.resolve())

    def action_back(self) -> None:
        self.dismiss(None)

    def action_toggle_all(self) -> None:
        tree = self.query_one(FilteredTree)
        tree.show_all = not tree.show_all
        tree.reload()
        self.update_folder()
