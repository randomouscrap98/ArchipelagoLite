"""
Full-screen file/folder picker for prompts.py, as its own process: the code asking for a file blocks until it has an
answer, often inside the client's event loop, so the picker can't run in that process's Textual app.
Usage: python -m aplite.pick_file OUT TITLE (file|folder) SUGGEST [SUFFIX ...]
Writes the chosen path to OUT; leaves it empty when cancelled.
"""
import os
import sys

from textual.app import App
from textual.binding import Binding

from aplite import prefs
from aplite.file_picker import FilePicker
import Utils


class PickerApp(App[str]):
    BINDINGS = [Binding("ctrl+p", "command_palette", "Options", show=False)]  # labelled like aplite's other apps

    def __init__(self, title: str, folder: bool, suggest: str, suffixes: list[str]) -> None:
        super().__init__()
        self.picker_title = title
        self.folder = folder
        self.suggest = suggest
        self.suffixes = suffixes

    def on_mount(self) -> None:
        prefs.bind_theme(self)
        suggested = os.path.expanduser(self.suggest) if self.suggest else ""
        start = suggested if os.path.isdir(suggested) else os.path.dirname(suggested)  # the caller's suggestion
        picker = FilePicker(self.picker_title, self.suffixes, "prompt_dir", Utils.user_path(), folders=self.folder,
                            start=start)
        self.push_screen(picker, lambda path: self.exit(str(path) if path else ""))


def main() -> None:
    out, title, kind, suggest, *suffixes = sys.argv[1:]
    path = PickerApp(title, kind == "folder", suggest, suffixes).run() or ""
    with open(out, "w", encoding="utf-8") as f:
        f.write(path)


if __name__ == "__main__":
    main()
