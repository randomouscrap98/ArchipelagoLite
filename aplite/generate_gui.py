"""
Generate GUI: pick player YAMLs from a file tree (left) into a list (right), then generate. Generate.py runs in a child
process (it refuses to start with worlds loaded); its log and progress show in a modal. File prompts the child needs
(missing ROMs) are relayed here and asked with the normal file picker (prompts.RELAY_ENV).
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import typing
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Label, OptionList, ProgressBar, RichLog
from textual.widgets.option_list import Option as ListOption

import Utils
import aplite
from aplite import prefs, prompts
from aplite.file_picker import FileBrowser, FilePicker, FilteredTree, PathInput, start_folder
from aplite.nav import AppliteApp

SUFFIXES = (".yaml", ".yml")
PREF_KEY = "generate_dir"

STAGES = ("Generating for", "Creating MultiWorld.", "Creating Items.", "Calculating Access Rules.",
          "Running Item Plando.", "Running Pre Main Fill.", "Filling the multiworld", "Running pre-output steps.",
          "Beginning output", "Calculating playthrough.", "Creating final archive", "Done.")
"""Log lines (Generate.py / Main.py) that mark progress, in order; the bar moves to the last one seen."""
ARCHIVE = "Creating final archive at "
LIST_GAMES = ("import json; from aplite import bytecode_cache; bytecode_cache.install(); "
              "from worlds.AutoWorld import AutoWorldRegister; "
              "print(json.dumps(sorted(AutoWorldRegister.world_types)))")
"""A fresh process's installed games (this one's loaded worlds can't be reloaded); the JSON is its last line."""


@dataclasses.dataclass
class Entry:
    path: Path
    players: list[tuple[str, str]]  # (name, game) per YAML document
    problems: list[str]

    def label(self) -> Text:
        who = ", ".join(f"{name} ({game})" for name, game in self.players) or "?"
        text = Text(f"{self.path.name}  {who}")
        if self.problems:
            text.stylize("bold red")
        return text


def installed_games() -> set[str]:
    """Games installed now, apworlds added since aplite started included. Slow (loads every world)."""
    import json
    out = subprocess.run([sys.executable, "-c", LIST_GAMES], env=aplite.child_env(), stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=True).stdout
    return set(json.loads(out.strip().splitlines()[-1]))


def check_yaml(path: Path, games: typing.Collection[str]) -> Entry:
    """Read a player YAML and check every game it can roll is in `games`."""
    entry = Entry(path, [], [])
    try:
        with open(path, encoding="utf-8-sig") as f:
            docs = [doc for doc in Utils.parse_yamls(f.read()) if doc is not None]
    except Exception as e:
        entry.problems.append(f"can't read it: {e}")
        return entry
    if not docs:
        entry.problems.append("no YAML documents in it")
    for doc in docs:
        if not isinstance(doc, dict):
            entry.problems.append("not a player YAML")
            continue
        game = doc.get("game")
        rolls = [g for g, weight in game.items() if weight] if isinstance(game, dict) else [game]
        name = str(doc.get("name", "?"))
        entry.players.append((name, " / ".join(map(str, rolls))))
        if not game:
            entry.problems.append(f"{name}: no game")
        for g in rolls:
            if g and g not in games:
                entry.problems.append(f"{name}: game {g!r} isn't installed (missing apworld? Ctrl+R rechecks)")
    return entry


class GenerateModal(ModalScreen[None]):
    """Runs Generate.py on the chosen YAMLs; can't be closed until it ends (Cancel stops it)."""
    CSS = """
    GenerateModal { align: center middle; }
    #box { width: 90%; height: 90%; padding: 0 1; border: thick $primary; background: $surface; }
    #status { padding: 0 1; }
    #gen-log { height: 1fr; }
    ProgressBar { padding: 0 1; }
    #buttons { height: auto; align-horizontal: right; }
    """
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, files: list[Path]) -> None:
        super().__init__()
        self.files = files
        self.proc: subprocess.Popen | None = None
        self.folder = ""  # copies of the YAMLs, while Generate runs
        self.stage = 0
        self.archive: str | None = None
        self.cancelled = False

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("Starting Generate…", id="status")
            yield ProgressBar(total=len(STAGES), show_eta=False)
            yield RichLog(id="gen-log", wrap=True, markup=False)
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel", variant="error")
                yield Button("Upload to Archipelago", id="upload", variant="primary")
                yield Button("Host here", id="selfhost")
                yield Button("Close", id="close", disabled=True)

    def on_mount(self) -> None:
        self.folder = tempfile.mkdtemp(prefix="aplite-generate-")
        for i, path in enumerate(self.files):  # numbered: two folders may hold the same file name
            shutil.copy(path, os.path.join(self.folder, f"{i + 1:02}_{path.name}"))
        env = {**aplite.child_env(), "PYTHONUNBUFFERED": "1", prompts.RELAY_ENV: "1"}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "aplite.script", Utils.local_path("Generate.py"),
             "--player_files_path", self.folder],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=self.read_output, daemon=True).start()
        self.query_one("#upload", Button).display = False  # once there's something to upload
        self.query_one("#selfhost", Button).display = False
        self.query_one("#cancel", Button).focus()

    def read_output(self) -> None:
        for line in self.proc.stdout:
            if line.startswith(prompts.RELAY_MARK):
                self.app.call_from_thread(self.ask, line[len(prompts.RELAY_MARK):])
            else:
                self.app.call_from_thread(self.log_line, line.rstrip("\n"))
        self.app.call_from_thread(self.finished, self.proc.wait())

    def log_line(self, line: str) -> None:
        self.query_one(RichLog).write(line)
        for i, mark in enumerate(STAGES[self.stage:], self.stage):
            if mark in line:
                self.stage = i + 1
                self.query_one(ProgressBar).progress = self.stage
                self.query_one("#status", Label).update(mark.rstrip("."))
                break
        if ARCHIVE in line:
            self.archive = line.split(ARCHIVE, 1)[1].strip()

    def ask(self, request: str) -> None:
        """A file prompt from the child: show the picker, send back the path (empty line: cancelled)."""
        title, folder, suffixes, suggest = prompts.relay_request(request)
        start = os.path.dirname(suggest) if suggest else None
        picker = FilePicker(title, suffixes, "prompt_dir", os.path.expanduser("~"), folders=folder, start=start)

        def answer(path: Path | None) -> None:
            try:
                self.proc.stdin.write(f"{path or ''}\n")
                self.proc.stdin.flush()
            except OSError:
                pass  # child already gone
        self.app.push_screen(picker, answer)

    def finished(self, status: int) -> None:
        shutil.rmtree(self.folder, ignore_errors=True)
        log = self.query_one(RichLog)
        if status == 0 and self.archive:
            self.query_one(ProgressBar).progress = len(STAGES)
            self.query_one("#status", Label).update("Done")
            log.write(Text(f"Generated: {self.archive}", style="bold green"))
            self.query_one("#upload", Button).display = True
            self.query_one("#selfhost", Button).display = True
        elif self.cancelled:
            self.query_one("#status", Label).update("Cancelled")
        else:
            self.query_one("#status", Label).update(f"Failed (exit {status})")
            log.write(Text("Generation failed; see the log above.", style="bold red"))
        self.proc = None
        self.query_one("#cancel", Button).disabled = True
        close = self.query_one("#close", Button)
        close.disabled = False
        upload = self.query_one("#upload", Button)
        (upload if upload.display else close).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "cancel":
            self.stop()
        elif event.button.id == "upload":
            from aplite.webhost import UploadModal
            # afterwards Close, not Upload again
            self.app.push_screen(UploadModal(Path(self.archive)), lambda _: self.query_one("#close", Button).focus())
        elif event.button.id == "selfhost":
            from aplite.selfhost.app import SelfHostScreen
            self.app.push_screen(SelfHostScreen(self.archive))
        else:
            self.action_close()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.cancelled = True
            self.proc.kill()

    def action_close(self) -> None:
        if self.proc is None:
            self.dismiss(None)

    def on_unmount(self) -> None:  # ctrl+q / ctrl+l while running
        self.stop()


class GenerateScreen(Screen):
    BINDINGS = [Binding("ctrl+t", "toggle_all", "Show all files"), Binding("ctrl+g", "generate", "Generate"),
                Binding("ctrl+r", "recheck", "Recheck"),
                Binding("delete", "remove", "Remove", show=False)]

    def __init__(self) -> None:
        super().__init__()
        self.start = start_folder(PREF_KEY, Utils.user_path("Players"))
        self.entries: list[Entry] = []
        from worlds.AutoWorld import AutoWorldRegister
        self.games: typing.Collection[str] = AutoWorldRegister.world_types
        self.rechecking = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="panes"):
            with Vertical(id="left"):
                yield Label("Files: Enter adds a YAML", classes="heading")
                yield FileBrowser(self.start, SUFFIXES, "Path to a YAML or folder. Enter opens, Ctrl+Space completes.")
            with Vertical(id="right"):
                yield Label("YAMLs to generate: Delete removes", classes="heading")
                yield OptionList(id="yamls")
                yield Button("Generate", id="generate", variant="primary")
        yield Label("Log", classes="heading")
        yield RichLog(id="log", wrap=True, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.update_folder()
        self.query_one(FilteredTree).focus()

    def update_folder(self) -> None:
        self.query_one(PathInput).value = str(self.query_one(FilteredTree).folder())

    def on_filtered_tree_folder_changed(self) -> None:
        self.update_folder()

    def log_line(self, text: str, style: str = "") -> None:
        self.query_one("#log", RichLog).write(Text(text, style=style))

    def on_file_browser_opened(self, event: FileBrowser.Opened) -> None:
        if event.path.is_file():
            self.add(event.path)
        else:
            self.log_line(f"Not found: {event.path}", "red")

    def add(self, path: Path) -> None:
        if any(entry.path == path for entry in self.entries):
            self.log_line(f"{path.name} is already in the list.", "yellow")
            return
        prefs.put(PREF_KEY, str(path.parent))
        entry = check_yaml(path, self.games)
        self.entries.append(entry)
        self.query_one("#yamls", OptionList).add_option(ListOption(entry.label()))
        self.report(entry, "Added")
        self.check_names()

    def report(self, entry: Entry, verb: str) -> None:
        if entry.problems:
            self.log_line(f"{entry.path.name}:", "bold red")
            for problem in entry.problems:
                self.log_line(f"  {problem}", "red")
        else:
            self.log_line(f"{verb} {entry.path.name}: " + ", ".join(f"{n} ({g})" for n, g in entry.players))

    def action_recheck(self) -> None:
        """Reload the installed games in a fresh process (apworlds installed meanwhile) and re-read every YAML."""
        if self.rechecking:
            return
        self.rechecking = True
        self.log_line("Rechecking installed games…")
        threading.Thread(target=self.load_games, daemon=True).start()

    def load_games(self) -> None:
        try:
            games, error = installed_games(), None
        except Exception as e:
            games, error = None, e
        self.app.call_from_thread(self.games_loaded, games, error)

    def games_loaded(self, games: set[str] | None, error: Exception | None) -> None:
        self.rechecking = False
        if games is None:
            self.log_line(f"Couldn't recheck the installed games: {error}", "red")
            return
        added = len(games - set(self.games))
        self.games = games
        self.log_line(f"{len(games)} games installed" + (f" ({added} new)." if added else "."))
        yamls = self.query_one("#yamls", OptionList)
        highlighted = yamls.highlighted
        self.entries = [check_yaml(entry.path, games) for entry in self.entries]
        yamls.clear_options()
        yamls.add_options([ListOption(entry.label()) for entry in self.entries])
        if highlighted is not None and self.entries:
            yamls.highlighted = min(highlighted, len(self.entries) - 1)
        for entry in self.entries:
            self.report(entry, "Checked")
        self.check_names()

    def check_names(self) -> None:
        names = [name.casefold() for entry in self.entries for name, _ in entry.players]
        for name in sorted({n for n in names if names.count(n) > 1}):
            self.log_line(f"Slot name {name!r} is used more than once; Generate will refuse that.", "yellow")

    def action_remove(self) -> None:
        yamls = self.query_one("#yamls", OptionList)
        if not yamls.has_focus or yamls.highlighted is None:
            return
        index = yamls.highlighted
        entry = self.entries.pop(index)
        yamls.remove_option_at_index(index)
        if self.entries:
            yamls.highlighted = min(index, len(self.entries) - 1)
        self.log_line(f"Removed {entry.path.name}.")

    def on_key(self, event) -> None:
        if event.key == "backspace" and self.query_one("#yamls").has_focus:
            event.stop()
            self.action_remove()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_generate()

    def action_generate(self) -> None:
        if not self.entries:
            self.log_line("Add at least one YAML first.", "yellow")
            return
        bad = [entry.path.name for entry in self.entries if entry.problems]
        if bad:
            self.log_line(f"Fix or remove first: {', '.join(bad)}", "red")
            return
        self.app.push_screen(GenerateModal([entry.path for entry in self.entries]))

    def action_toggle_all(self) -> None:
        self.query_one(FilteredTree).toggle_all()


class GenerateApp(AppliteApp):
    TITLE = "Generate GUI"
    CSS = """  /* here, not on GenerateScreen: a default screen's own CSS isn't loaded */
    #panes { height: 1fr; }
    #left { width: 1fr; }
    FileBrowser { height: 1fr; }
    #right { width: 1fr; }
    #yamls { height: 1fr; }
    FilteredTree { height: 1fr; border: tall $border-blurred; }  /* same box as the lists, so focus shows */
    FilteredTree:focus { border: tall $border; }
    .heading { padding: 0 1; }
    #generate { width: 100%; }
    #log { height: 10; border: tall $border-blurred; }
    #log:focus { border: tall $border; }
    """

    def get_default_screen(self) -> Screen:
        return GenerateScreen()


def run(*args: str) -> None:
    GenerateApp().run()


def component():
    """Launcher entry for this tool (added to the component list by aplite)."""
    from worlds.LauncherComponents import Component, Type
    return Component("Generate GUI", func=run, component_type=Type.TOOL,
                     description="Pick player YAMLs and generate a multiworld, with the log on screen.")
