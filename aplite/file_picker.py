"""File picker screen: dismisses with the chosen path, or None to go back. Remembers the folder in a pref. Its file tree
+ path input (FileBrowser) is also Generate GUI's."""
from __future__ import annotations

import os
import shlex
import typing
import urllib.parse
from pathlib import Path

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import DirectoryTree, Footer, Input, Static

from aplite import prefs


def start_folder(pref_key: str, default_dir: str, start: str | None = None) -> Path:
    """Where a file tree opens: `start` if given, else the folder saved under `pref_key`, else `default_dir`."""
    last = prefs.get(pref_key)
    return Path(start if start and os.path.isdir(start) else last if last and os.path.isdir(last) else default_dir)


class FilteredTree(DirectoryTree):
    """Tree keys (research/keyboard.md): Right opens a folder / goes into it, Left closes it / goes up. Posts
    FolderChanged when it shows another folder."""
    BINDINGS = [Binding("backspace", "parent", "Parent folder"), Binding("right", "open_node", show=False),
                Binding("left", "close_node", show=False)]

    class FolderChanged(Message):
        def __init__(self, path: Path) -> None:
            super().__init__()
            self.path = path

    def __init__(self, path: Path, suffixes: typing.Sequence[str], show_all: bool = False) -> None:
        super().__init__(path)
        self.suffixes = tuple(suffixes)
        self.show_all = show_all

    def filter_paths(self, paths: typing.Iterable[Path]) -> typing.Iterable[Path]:
        return [p for p in paths if self.shows(p)]

    def shows(self, path: Path) -> bool:
        if path.name == "..":
            return True
        return not path.name.startswith(".") and \
            (path.is_dir() or self.show_all or path.name.endswith(self.suffixes))

    def _directory_content(self, location: Path, worker) -> typing.Iterator[Path]:
        # private DirectoryTree method (no public hook): adds ".." to the root folder; sorting puts it first
        root = Path(self.path).expanduser().resolve()
        if location == root and root.parent != root:
            yield location / ".."
        yield from super()._directory_content(location, worker)

    def _populate_node(self, node, content) -> None:
        # private DirectoryTree method: a folder's first expansion fills it here, after NodeExpanded
        super()._populate_node(node, content)
        if not node.is_root:
            self.call_after_refresh(self.reveal, node)

    def on_tree_node_expanded(self, event) -> None:
        if event.node.children and not event.node.is_root:  # expanded again: already filled
            self.call_after_refresh(self.reveal, event.node)

    def reveal(self, node) -> None:
        """Scroll an expanded folder's contents into view; if they don't fit, put the folder at the top."""
        last = node
        while last.is_expanded and last.children:
            last = last.children[-1]
        first, end = node.line, last.line
        height = self.scrollable_content_region.height
        if first < 0 or height <= 0:
            return
        if end - first + 1 > height or first < self.scroll_y:
            self.scroll_to(y=first, animate=False)
        elif end >= self.scroll_y + height:
            self.scroll_to(y=end - height + 1, animate=False)

    def action_open_node(self) -> None:
        node = self.cursor_node
        if node is None or node.data is None or not node.allow_expand:
            return
        if node.data.path.name == "..":
            self.action_parent()
        elif not node.is_expanded:
            node.expand()
        elif node.children:
            self.move_cursor(node.children[0])

    def action_close_node(self) -> None:
        node = self.cursor_node
        if node is not None and node.allow_expand and node.is_expanded and not node.is_root:
            node.collapse()
        elif node is not None and node.parent is not None and not node.parent.is_root:
            self.move_cursor(node.parent)
        else:
            self.action_parent()

    def folder(self) -> Path:
        return Path(self.path).resolve()

    def go_to(self, folder: Path) -> None:
        self.path = folder
        self.post_message(self.FolderChanged(folder))

    def action_parent(self) -> None:
        self.go_to(self.folder().parent)

    def toggle_all(self) -> None:
        self.show_all = not self.show_all
        self.reload()

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        if event.path.name == "..":
            event.stop()
            self.action_parent()


def dropped_path(text: str) -> Path | None:
    """The existing file or folder a paste names, if any. Terminals paste a file dropped on them as its path: plain,
    quoted, shell-escaped, or a file:// URI depending on the terminal. Absolute paths only, so pasting a word isn't
    taken for a file in the current folder."""
    text = text.strip().splitlines()[0].strip() if text.strip() else ""
    candidates = [text, text.strip("'\"")]
    if text.startswith("file://"):
        candidates.append(urllib.parse.unquote(urllib.parse.urlparse(text).path))
    try:
        candidates += shlex.split(text, posix=os.name != "nt")[:1]  # first of several dropped files
    except ValueError:
        pass
    for candidate in candidates:
        path = Path(os.path.expanduser(candidate))
        if candidate and path.is_absolute() and path.exists():
            return path
    return None


def _not_hidden(path: Path) -> bool:
    return not path.name.startswith(".")


class PathInput(Input):
    """Ctrl+Space completes the path like a shell: as far as the matches agree, then further presses cycle through them
    in place. Tab moves focus as usual (research/keyboard.md). Terminals send Ctrl+Space as NUL (ctrl+@). A file
    dropped on it replaces the value and posts Dropped."""
    BINDINGS = [Binding("ctrl+space,ctrl+@", "complete", "Complete path", show=False)]

    class Dropped(Message):
        def __init__(self, path: Path) -> None:
            super().__init__()
            self.path = path

    def __init__(self, *args, shows: typing.Callable[[Path], bool] = _not_hidden, **kwargs) -> None:
        """`shows` decides which entries completion offers."""
        super().__init__(*args, **kwargs)
        self.shows = shows
        self.cycle: list[str] = []  # completions Ctrl+Space is cycling through; reset once the value is edited otherwise

    def _on_paste(self, event: events.Paste) -> None:
        """A dropped file replaces the value (cleaned up)."""
        path = dropped_path(event.text)
        if path is None:
            super()._on_paste(event)
            return
        event.stop()
        self.value = str(path)
        self.cursor_position = len(self.value)
        self.post_message(self.Dropped(path))

    def action_complete(self) -> None:
        if self.value in self.cycle:  # cycling: next match
            self.set_completion(self.cycle[(self.cycle.index(self.value) + 1) % len(self.cycle)])
            return
        self.cycle = []
        typed = os.path.expanduser(self.value)
        folder, prefix = os.path.split(typed)
        folder = folder or "."
        try:
            names = [entry.name for entry in os.scandir(folder) if entry.name.startswith(prefix)]
        except OSError:
            return
        matches = sorted(name for name in names if self.shows(Path(folder, name)))
        if not matches:
            self.app.bell()
            return
        shown_folder = folder if folder != "." or typed.startswith(".") else ""
        common = os.path.join(shown_folder, os.path.commonprefix(matches))
        if len(matches) == 1 and os.path.isdir(common):
            common += os.sep
        if common != typed:
            self.set_completion(common)
        elif len(matches) > 1:  # nothing more in common: start cycling through the matches
            self.cycle = [os.path.join(shown_folder, name) + (os.sep if os.path.isdir(os.path.join(folder, name))
                                                              else "") for name in matches]
            self.set_completion(self.cycle[0])

    def set_completion(self, value: str) -> None:
        self.value = value
        self.cursor_position = len(value)


class FileBrowser(Vertical):
    """A FilteredTree over a PathInput showing the highlighted entry's path. Enter on a file, a typed or dropped path
    (a folder only with `pick_folders`; otherwise the tree shows it) post Opened: the screen decides what that does
    (the path may not exist)."""

    class Opened(Message):
        def __init__(self, path: Path) -> None:
            super().__init__()
            self.path = path

    def __init__(self, start: Path, suffixes: typing.Sequence[str], placeholder: str, show_all: bool = False,
                 pick_folders: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.start = start
        self.suffixes = suffixes
        self.placeholder = placeholder
        self.show_all = show_all
        self.pick_folders = pick_folders

    def compose(self) -> ComposeResult:
        tree = FilteredTree(self.start, self.suffixes, self.show_all)
        yield tree
        yield PathInput(placeholder=self.placeholder, shows=tree.shows)

    @property
    def tree(self) -> FilteredTree:
        return self.query_one(FilteredTree)

    @property
    def path_input(self) -> PathInput:
        return self.query_one(PathInput)

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        event.stop()
        self.post_message(self.Opened(event.path.resolve()))

    def on_tree_node_highlighted(self, event) -> None:
        entry = event.node.data
        if entry is not None:
            self.path_input.value = str(entry.path.resolve())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()  # Input.Submitted bubbles to the App otherwise
        self.open(Path(os.path.expanduser(event.value.strip())))

    def on_path_input_dropped(self, event: PathInput.Dropped) -> None:
        event.stop()
        self.open(event.path)

    def on_paste(self, event: events.Paste) -> None:
        """A file dropped on the terminal while the tree has focus (the path input handles its own)."""
        if path := dropped_path(event.text):
            event.stop()
            self.open(path)

    def open(self, path: Path) -> None:
        path = path.resolve()
        if path.is_dir() and not self.pick_folders:
            self.tree.go_to(path)
            self.tree.focus()
        else:
            self.post_message(self.Opened(path))


class FilePicker(Screen[Path | None]):
    CSS = """
    #folder { height: 1; padding: 0 1; background: $panel; }
    FilteredTree { height: 1fr; }
    """
    BINDINGS = [Binding("escape", "back", "Back"), Binding("ctrl+t", "toggle_all", "Show all files"),
                Binding("ctrl+l", "use_folder", "Use this folder")]

    def __init__(self, title: str, suffixes: typing.Sequence[str], pref_key: str, default_dir: str,
                 folders: bool = False, show_all: bool = False, start: str | None = None) -> None:
        """Starts in `start` if given, else the folder saved under `pref_key`, else `default_dir`; only shows `suffixes`
        until Ctrl+T (or everything with `show_all`). With `folders`, it picks a folder: Ctrl+L uses the shown one,
        Enter on a folder path picks it."""
        super().__init__()
        self.folders = folders
        self.show_all = show_all
        self.title_text = title
        self.suffixes = suffixes
        self.pref_key = pref_key
        self.start = start_folder(pref_key, default_dir, start)

    def compose(self) -> ComposeResult:
        yield Static(id="folder")
        yield FileBrowser(self.start, self.suffixes, "Path to a file or folder. Enter opens, Ctrl+Space completes.",
                          show_all=self.show_all, pick_folders=self.folders)
        yield Footer()

    def on_mount(self) -> None:
        self.update_folder()
        self.query_one(FilteredTree).focus()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        return self.folders if action == "use_folder" else True

    def action_use_folder(self) -> None:
        self.choose_folder(self.query_one(FilteredTree).folder())

    def choose_folder(self, path: Path) -> None:
        prefs.put(self.pref_key, str(path))
        self.dismiss(path)

    def update_folder(self) -> None:
        tree = self.query_one(FilteredTree)
        shown = "all files" if tree.show_all or not self.suffixes else ", ".join(self.suffixes)
        self.query_one("#folder", Static).update(f"{self.title_text}: {tree.folder()}  ({shown})")

    def on_filtered_tree_folder_changed(self) -> None:
        self.update_folder()

    def on_file_browser_opened(self, event: FileBrowser.Opened) -> None:
        """A file is chosen; a folder only arrives when picking folders."""
        if event.path.is_dir():
            self.choose_folder(event.path)
        elif event.path.is_file():
            self.choose(event.path)
        else:
            self.notify(f"Not found: {event.path}", severity="error")

    def choose(self, path: Path) -> None:
        prefs.put(self.pref_key, str(path.resolve().parent))
        self.dismiss(path.resolve())

    def action_back(self) -> None:
        self.dismiss(None)

    def action_toggle_all(self) -> None:
        self.query_one(FilteredTree).toggle_all()
        self.update_folder()
