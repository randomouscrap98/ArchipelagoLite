"""Launcher screen for Open Patch: pick a patch file; dismisses with its path, or None to go back."""
from __future__ import annotations

import os
import typing
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DirectoryTree, Footer, Input, Static

import Utils
from aplite import prefs


def patch_suffixes() -> list[str]:
    """Extensions of files some client can open; same list as upstream's open_patch."""
    from worlds.LauncherComponents import SuffixIdentifier, Type, components
    return sorted({suffix for c in components
                   if c.type == Type.CLIENT and isinstance(c.file_identifier, SuffixIdentifier)
                   for suffix in c.file_identifier.suffixes})


def start_dir() -> Path:
    last = prefs.get("patch_dir")
    if last and os.path.isdir(last):
        return Path(last)
    return Path(Utils.local_path())


class PatchTree(DirectoryTree):
    BINDINGS = [Binding("backspace", "parent", "Parent folder")]

    def __init__(self, path: Path, suffixes: list[str]) -> None:
        super().__init__(path)
        self.suffixes = tuple(suffixes)
        self.show_all = False

    def filter_paths(self, paths: typing.Iterable[Path]) -> typing.Iterable[Path]:
        return [p for p in paths if not p.name.startswith(".") and
                (p.is_dir() or self.show_all or p.name.endswith(self.suffixes))]

    def action_parent(self) -> None:
        self.path = Path(self.path).resolve().parent
        self.screen.update_folder()


class PatchPicker(Screen[Path | None]):
    CSS = """
    #folder { height: 1; padding: 0 1; background: $panel; }
    PatchTree { height: 1fr; }
    """
    BINDINGS = [Binding("escape", "back", "Back"), Binding("ctrl+t", "toggle_all", "Show all files")]

    def compose(self) -> ComposeResult:
        yield Static(id="folder")
        yield PatchTree(start_dir(), patch_suffixes())
        yield Input(placeholder="Path to a patch file or folder. Enter opens; Tab switches between list and path.")
        yield Footer()

    def on_mount(self) -> None:
        self.update_folder()
        self.query_one(PatchTree).focus()

    def update_folder(self) -> None:
        tree = self.query_one(PatchTree)
        shown = "all files" if tree.show_all else "patch files"
        self.query_one("#folder", Static).update(f"Open patch: {Path(tree.path).resolve()}  ({shown})")

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self.choose(event.path)

    def on_tree_node_highlighted(self, event) -> None:
        entry = event.node.data
        if entry is not None:
            self.query_one(Input).value = str(entry.path)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()  # the launcher app below handles Input.Submitted too
        path = Path(os.path.expanduser(event.value.strip()))
        tree = self.query_one(PatchTree)
        if path.is_dir():
            tree.path = path
            self.update_folder()
            tree.focus()
        elif path.is_file():
            self.choose(path)
        else:
            self.notify(f"Not found: {path}", severity="error")

    def choose(self, path: Path) -> None:
        prefs.set("patch_dir", str(path.resolve().parent))
        self.dismiss(path.resolve())

    def action_back(self) -> None:
        self.dismiss(None)

    def action_toggle_all(self) -> None:
        tree = self.query_one(PatchTree)
        tree.show_all = not tree.show_all
        tree.reload()
        self.update_folder()
