"""
Generate GUI: pick player YAMLs from a file tree (left) into a list (right), then generate. Generate.py runs in a child
process (it refuses to start with worlds loaded); its log and progress show in a modal. File prompts the child needs
(missing ROMs) are relayed here and asked with the normal file picker (prompts.RELAY_ENV).
"""
from __future__ import annotations

import dataclasses
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import typing
from pathlib import Path

from rich.console import Console
from rich.table import Table
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
from aplite.combo import keep_scroll
from aplite.file_picker import (SORT_BINDING, FileBrowser, FilteredTree, PathInput, cycle_sort, prompt_picker,
                                start_folder)
from aplite.nav import AppliteApp

SUFFIXES = (".yaml", ".yml")
PREF_KEY = "generate_dir"

STAGES = ("Generating for", "Creating MultiWorld.", "Creating Items.", "Calculating Access Rules.",
          "Running Item Plando.", "Running Pre Main Fill.", "Filling the multiworld", "Running pre-output steps.",
          "Beginning output", "Calculating playthrough.", "Creating final archive", "Done.")
"""Log lines (Generate.py / Main.py) that mark progress, in order; the bar moves to the last one seen."""
ARCHIVE = "Creating final archive at "
GAME_ROW = re.compile(r"^(?:\[[^]]*\] )? (?P<game>.+?)\s*: v(?P<version>\S+)\s*\| Items:\s*(?P<items>\d+) \| "
                      r"Locations:\s*(?P<locations>\d+)\s*$")
"""Main.py's per-game summary line; padded to the longest installed game name, so it's shown as one table."""
LIST_GAMES = ("import json; from aplite import bytecode_cache; bytecode_cache.install(); "
              "from worlds.AutoWorld import AutoWorldRegister; "
              "print(json.dumps(sorted(AutoWorldRegister.world_types)))")
"""A fresh process's installed games (this one's loaded worlds can't be reloaded); the JSON is its last line."""


def duplicate_names(names: typing.Iterable[str]) -> list[str]:
    """Slot names Generate refuses because more than one player has them (casefolded). Templated names ({player},
    {number}, ...) don't count: Generate numbers them."""
    names = [name.strip().casefold() for name in names if name.strip() and "{" not in name]
    return sorted({name for name in names if names.count(name) > 1})


@dataclasses.dataclass
class Entry:
    path: Path
    players: list[tuple[str, str]]  # (name, game) per YAML document
    problems: list[str]
    meta: str | None = None  # meta_description, for a meta file (overrides the player YAMLs' options)

    def label(self) -> Text:
        if self.meta is not None:
            who = f"meta: {self.meta}"
        else:
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


def host_meta() -> Path | None:
    """The meta file host.yaml points at, if it exists: Generate.py applies it to every run when nothing overrides it."""
    import settings
    generator = settings.get_settings().generator
    path = Path(generator.meta_file_path)
    if not path.is_absolute():  # Generate.py: relative to the player files folder
        path = Path(generator.player_files_path.resolve()) / path
    return path if path.is_file() else None


def player_files() -> tuple[Path, list[Path]]:
    """host.yaml's player files folder and the files Generate.py reads from it: any name (not everyone uses .yaml)
    but dotfiles, .ini and the meta/weights files, no subfolders."""
    import settings
    generator = settings.get_settings().generator
    folder = Path(generator.player_files_path.resolve())
    skip = {(folder / name).resolve() for name in (generator.meta_file_path, generator.weights_file_path)}
    if not folder.is_dir():
        return folder, []
    return folder, sorted(f for f in folder.iterdir() if f.is_file() and not f.name.startswith(".")
                          and not f.name.lower().endswith(".ini") and f.resolve() not in skip)


def meta_plan(entries: list[Entry]) -> tuple[Entry | None, Path | None, str | None]:
    """Which meta file Generate applies: (the picked meta YAML, host.yaml's meta file, error). host.yaml's is None
    when it's the picked file itself; with both set, the picked one replaces it."""
    metas = [entry for entry in entries if entry.meta is not None]
    if len(metas) > 1:
        return None, None, f"Only one meta file allowed: {', '.join(entry.path.name for entry in metas)}"
    picked = metas[0] if metas else None
    host = host_meta()
    if picked and host and host.resolve() == picked.path.resolve():
        host = None
    return picked, host, None


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
    if docs and isinstance(docs[-1], dict) and "meta_description" in docs[-1]:  # Generate.py reads the last one
        entry.meta = str(docs[-1]["meta_description"])
        return entry
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
    #gen-log { height: 1fr; border: tall $border-blurred; }
    #gen-log:focus { border: tall $border; }
    ProgressBar { padding: 0 1; }
    #buttons { height: auto; align-horizontal: right; }
    """
    BINDINGS = [Binding("escape", "close", "Close"), Binding("ctrl+c", "copy_log", "Copy log")]

    def __init__(self, files: list[Path], meta: Path | None = None) -> None:
        """`meta`: the meta file to apply (meta_plan: picked, or host.yaml's), copied in as meta.yaml."""
        super().__init__()
        self.files = files
        self.meta = meta
        self.proc: subprocess.Popen | None = None
        self.folder = ""  # copies of the YAMLs, while Generate runs
        self.stage = 0
        self.game_rows: list[tuple[str, ...]] = []
        self.log_text: list[str] = []  # the log as plain text, for Copy log
        self.archive: str | None = None
        self.cancelled = False

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("Starting Generate…", id="status")
            yield ProgressBar(total=len(STAGES), show_eta=False)
            yield RichLog(id="gen-log", wrap=True, markup=False, min_width=0)  # wrap at its width, not 78
            with Horizontal(id="buttons"):
                yield Button("Copy log", id="copy")
                yield Button("Cancel", id="cancel", variant="error")
                yield Button("Upload to Archipelago", id="upload", variant="primary")
                yield Button("Host here", id="selfhost")
                yield Button("Close", id="close", disabled=True)

    def on_mount(self) -> None:
        self.folder = tempfile.mkdtemp(prefix="aplite-generate-")
        for i, path in enumerate(self.files):  # numbered: two folders may hold the same file name
            shutil.copy(path, os.path.join(self.folder, f"{i + 1:02}_{path.name}"))
        meta = "meta.yaml"  # relative to the copies; a missing file means no meta
        if self.meta:
            shutil.copy(self.meta, os.path.join(self.folder, meta))
        env = {**aplite.child_env(), "PYTHONUNBUFFERED": "1", prompts.RELAY_ENV: "1"}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "aplite.script", Utils.local_path("Generate.py"),
             "--player_files_path", self.folder, "--meta_file_path", meta],
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
        if row := GAME_ROW.match(line):
            self.game_rows.append(row.group("game", "version", "items", "locations"))
            return
        self.flush_games()
        self.write(line)
        for i, mark in enumerate(STAGES[self.stage:], self.stage):
            if mark in line:
                self.stage = i + 1
                self.query_one(ProgressBar).progress = self.stage
                self.query_one("#status", Label).update(mark.rstrip("."))
                break
        if ARCHIVE in line:
            self.archive = line.split(ARCHIVE, 1)[1].strip()

    def flush_games(self) -> None:
        """The buffered summary lines as a table fit to the log's width (long game names wrap)."""
        if not self.game_rows:
            return
        table = Table(box=None, padding=(0, 1), header_style="bold")
        table.add_column("Game", min_width=10)
        table.add_column("Version", overflow="fold")
        table.add_column("Items", justify="right", no_wrap=True)
        table.add_column("Locations", justify="right", no_wrap=True)
        for row in self.game_rows:
            table.add_row(*row)
        self.game_rows = []
        self.write(table)

    def write(self, content: str | Text | Table) -> None:
        self.query_one(RichLog).write(content)
        if isinstance(content, Table):
            console = Console(file=io.StringIO(), width=120, color_system=None)
            console.print(content)
            self.log_text.extend(line.rstrip() for line in console.file.getvalue().splitlines())
        else:
            self.log_text.append(str(content))

    def action_copy_log(self) -> None:
        """Copies through the terminal (OSC 52): Windows Terminal, kitty, WezTerm, iTerm2; not GNOME Terminal."""
        self.app.copy_to_clipboard("\n".join(self.log_text) + "\n")
        status = self.query_one("#status", Label)
        shown = status.content
        status.update("Copied the log to the clipboard")
        self.set_timer(2, lambda: status.update(shown) if status.content == "Copied the log to the clipboard" else None)

    def ask(self, request: str) -> None:
        """A file prompt from the child: show the picker, send back the path (empty line: cancelled)."""
        title, folder, suffixes, suggest = prompts.relay_request(request)
        picker = prompt_picker(title, folder, suffixes, suggest)

        def answer(path: Path | None) -> None:
            try:
                self.proc.stdin.write(f"{path or ''}\n")
                self.proc.stdin.flush()
            except OSError:
                pass  # child already gone
        self.app.push_screen(picker, answer)

    def finished(self, status: int) -> None:
        self.flush_games()
        shutil.rmtree(self.folder, ignore_errors=True)
        if status == 0 and self.archive:
            self.query_one(ProgressBar).progress = len(STAGES)
            self.query_one("#status", Label).update("Done")
            self.write(Text(f"Generated: {self.archive}", style="bold green"))
            self.query_one("#upload", Button).display = True
            self.query_one("#selfhost", Button).display = True
        elif self.cancelled:
            self.query_one("#status", Label).update("Cancelled")
        else:
            self.query_one("#status", Label).update(f"Failed (exit {status})")
            self.write(Text("Generation failed; see the log above.", style="bold red"))
        self.proc = None
        self.query_one("#cancel", Button).disabled = True
        close = self.query_one("#close", Button)
        close.disabled = False
        upload = self.query_one("#upload", Button)
        (upload if upload.display else close).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "copy":
            self.action_copy_log()
        elif event.button.id == "cancel":
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
    BINDINGS = [Binding("ctrl+t", "toggle_all", "Show all files"), SORT_BINDING,
                Binding("ctrl+g", "generate", "Generate"), Binding("ctrl+r", "recheck", "Recheck"),
                Binding("ctrl+o", "add_players", "Add Players folder"), Binding("delete", "remove", "Remove", show=False)]

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
                yield Label(id="meta")
                yield Button("Generate", id="generate", variant="primary")
        yield Label("Log", classes="heading")
        yield RichLog(id="log", wrap=True, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.update_folder()
        self.update_meta()
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
        if self.listed(path):
            self.log_line(f"{path.name} is already in the list.", "yellow")
            return
        prefs.put(PREF_KEY, str(path.parent))
        self.append(path)
        self.check_names()
        self.update_meta()

    def action_add_players(self) -> None:
        """Every file Generate (old) would read from host.yaml's player files folder that isn't listed yet."""
        folder, files = player_files()
        if not folder.is_dir():
            self.log_line(f"No player files folder: {folder}", "red")
            return
        new = [path for path in files if not self.listed(path)]
        for path in new:
            self.append(path)
        skipped = len(files) - len(new)
        self.log_line(f"Added {len(new)} from {folder.name}" + (f" ({skipped} already listed)." if skipped else "."))
        self.check_names()
        self.update_meta()

    def listed(self, path: Path) -> bool:
        return any(entry.path.resolve() == path.resolve() for entry in self.entries)

    def append(self, path: Path) -> None:
        entry = check_yaml(path, self.games)
        self.entries.append(entry)
        self.query_one("#yamls", OptionList).add_option(ListOption(entry.label()))
        self.report(entry, "Added")

    def report(self, entry: Entry, verb: str) -> None:
        if entry.problems:
            self.log_line(f"{entry.path.name}:", "bold red")
            for problem in entry.problems:
                self.log_line(f"  {problem}", "red")
        elif entry.meta is not None:
            self.log_line(f"{verb} {entry.path.name} (meta: {entry.meta})")
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
        keep_scroll(yamls)
        yamls.clear_options()
        yamls.add_options([ListOption(entry.label()) for entry in self.entries])
        if highlighted is not None and self.entries:
            yamls.highlighted = min(highlighted, len(self.entries) - 1)
        for entry in self.entries:
            self.report(entry, "Checked")
        self.check_names()
        self.update_meta()

    def check_names(self) -> None:
        for name in duplicate_names(name for entry in self.entries for name, _ in entry.players):
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
        self.update_meta()

    def update_meta(self) -> None:
        """One line under the list: the meta file Generate will apply (picked, or host.yaml's), if any."""
        label = self.query_one("#meta", Label)
        picked, host, error = meta_plan(self.entries)
        if error:
            text = Text("Only one meta file allowed", "red")
        elif picked and host:
            text = Text(f"Meta: {picked.path.name}, replaces host.yaml's {host.name}: {picked.meta}", "yellow")
        elif picked:
            text = Text(f"Meta: {picked.path.name}: {picked.meta}", "yellow")
        elif host:
            text = Text(f"Meta: {host.name} (from host.yaml): {check_yaml(host, self.games).meta}", "yellow")
        else:
            label.display = False
            return
        label.update(text)
        label.display = True

    def on_key(self, event) -> None:
        if event.key == "backspace" and self.query_one("#yamls").has_focus:
            event.stop()
            self.action_remove()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_generate()

    def action_generate(self) -> None:
        players = [entry for entry in self.entries if entry.meta is None]
        if not players:
            self.log_line("Add at least one player YAML first.", "yellow")
            return
        bad = [entry.path.name for entry in self.entries if entry.problems]
        if bad:
            self.log_line(f"Fix or remove first: {', '.join(bad)}", "red")
            return
        picked, host, error = meta_plan(self.entries)
        if error:
            self.log_line(error, "red")
            return
        if picked and host:
            self.log_line(f"{picked.path.name} replaces the host.yaml meta file {host}", "yellow")
        elif host:
            self.log_line(f"Using the host.yaml meta file {host}", "yellow")
        self.app.push_screen(GenerateModal([entry.path for entry in players], picked.path if picked else host))

    def action_toggle_all(self) -> None:
        self.query_one(FilteredTree).toggle_all()

    def action_cycle_sort(self) -> None:
        cycle_sort(self)


class GenerateApp(AppliteApp):
    TITLE = "Generate Seed"
    CSS = """  /* here, not on GenerateScreen: a default screen's own CSS isn't loaded */
    #panes { height: 1fr; }
    #left { width: 1fr; }
    FileBrowser { height: 1fr; }
    #right { width: 1fr; }
    #yamls { height: 1fr; }
    #meta { padding: 0 1; text-wrap: nowrap; text-overflow: ellipsis; }
    .tree-box { height: 1fr; border: tall $border-blurred; }  /* same box as the lists, so focus shows */
    .tree-box:focus-within { border: tall $border; }
    FilteredTree { height: 1fr; }
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
    return Component("Generate Seed", func=run, component_type=Type.TOOL,
                     description="Pick player YAMLs and generate a multiworld, with the log on screen.")
