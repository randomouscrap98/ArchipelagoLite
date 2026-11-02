"""
Terminal Options Creator (see master_detail.py for the layout). Loads existing YAMLs and keeps their unrelated content.
"""
from __future__ import annotations

import copy
import functools
import os
import typing
from inspect import cleandoc
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Footer, Input, Label, OptionList, Select, SelectionList, Static, TextArea
from textual.widgets.option_list import Option as ListOption

import Utils
from aplite import prefs
from aplite.file_picker import FilePicker
from aplite.master_detail import CSS as MASTER_DETAIL_CSS, MasterDetailApp, describe, to_yaml

if typing.TYPE_CHECKING:
    from Options import Option
    from worlds.AutoWorld import World

MAX_NAME = 16  # Generate rejects longer slot names
DEFAULT_DIR = Utils.user_path("Players")

# editor kinds (see kind())
CHOICE_KINDS = frozenset({"choice", "textchoice"})
RANGE_KINDS = frozenset({"range", "named"})
LIST_KINDS = frozenset({"set", "list"})
MAPPING_KINDS = frozenset({"counter", "dict"})


@functools.cache
def kind(option: type[Option]) -> str:
    """Which editor an option gets. Checks the MRO directly: issubclass on option classes goes through ABC machinery
    that walks thousands of subclasses on first use (~2.8ms per option, ~0.3s per game)."""
    from Options import (Choice, FreeText, NamedRange, OptionCounter, OptionDict, OptionList as OptList, OptionSet,
                         Range, TextChoice, Toggle)
    bases = set(option.__mro__)
    if TextChoice in bases:
        return "textchoice"
    if Choice in bases or Toggle in bases:
        return "choice"
    if NamedRange in bases and option.special_range_names:
        return "named"
    if Range in bases:
        return "range"
    if FreeText in bases:
        return "text"
    if OptionSet in bases or OptList in bases:
        return "set" if option.valid_keys else "list"
    if OptionCounter in bases:
        return "counter"
    if OptionDict in bases:
        return "dict"
    return "other"


@functools.cache
def _default_value(option: type[Option]) -> typing.Any:
    default = option.default
    if isinstance(default, (set, frozenset)):
        return sorted(default)
    if isinstance(default, (list, tuple)):
        return list(default)
    if isinstance(default, dict):
        return dict(default)
    if kind(option) in CHOICE_KINDS and isinstance(default, int):
        return option.name_lookup.get(default, default)
    return default


def default_value(option: type[Option]) -> typing.Any:
    """The option's default as it would be written in a YAML (a fresh copy, safe to modify)."""
    return copy.copy(_default_value(option))


def can_randomize(option: type[Option]) -> bool:
    return option.supports_weighting and kind(option) != "text"


def is_random(value: typing.Any) -> bool:
    return isinstance(value, str) and value.startswith("random")


RANDOM_BIASES = {"": "Even", "low": "Low", "middle": "Middle", "high": "High"}


def parse_random(value: str) -> tuple[str, int | None, int | None]:
    """'random[-range][-bias][-min-max]' (see Options._RANDOM_OPTS) -> (bias, min, max); "" bias is even."""
    parts = value.split("-")[1:]
    low = high = None
    if parts[:1] == ["range"] and len(parts) >= 3:
        try:
            low, high = sorted((int(parts[-2]), int(parts[-1])))
        except ValueError:
            pass
        parts = parts[1:-2]
    bias = parts[0] if parts and parts[0] in RANDOM_BIASES else ""
    return bias, low, high


def random_text(bias: str, low: int | None = None, high: int | None = None) -> str:
    """Inverse of parse_random."""
    parts = ["random"] + (["range"] if low is not None else []) + ([bias] if bias else [])
    if low is not None:
        parts += [str(low), str(high)]
    return "-".join(parts)


def is_weighted(option: type[Option], value: typing.Any) -> bool:
    """A {value: weight} dict from a loaded YAML, for an option whose plain value isn't a dict."""
    return isinstance(value, dict) and kind(option) not in MAPPING_KINDS


def normalize(option: type[Option], value: typing.Any) -> typing.Any:
    """Loaded YAML value -> editor value: {x: 50, y: 0} -> x (real weighted mixes kept); YAML booleans -> names."""
    if is_weighted(option, value):
        nonzero = [key for key, weight in value.items() if weight]
        if len(nonzero) != 1:
            return value
        value = nonzero[0]
    if isinstance(value, bool) and kind(option) in CHOICE_KINDS:
        return option.name_lookup.get(int(value), str(value).lower())
    return value


def game_options(world: type[World]) -> dict[str, dict[str, type[Option]]]:
    """Option groups as the website's advanced options page shows them."""
    from Options import Visibility, get_option_groups
    return {group: options for group, options in get_option_groups(world, Visibility.complex_ui).items() if options}


class SaveScreen(ModalScreen[Path | None]):
    CSS = """
    SaveScreen { align: center middle; }
    #box { width: 90%; height: auto; padding: 1 2; border: thick $primary; background: $surface; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, default: str) -> None:
        super().__init__()
        self.default = default
        self.confirm_overwrite: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("Save options as (Enter saves, Esc cancels):")
            yield Input(self.default, id="path")
            yield Label("", id="warning")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        path = os.path.expanduser(event.value.strip())
        if os.path.exists(path) and self.confirm_overwrite != path:
            self.confirm_overwrite = path
            self.query_one("#warning", Label).update("File exists. Press Enter again to overwrite.")
            return
        self.dismiss(Path(path))

    def on_input_changed(self, event: Input.Changed) -> None:
        self.confirm_overwrite = None
        self.query_one("#warning", Label).update("")

    def action_cancel(self) -> None:
        self.dismiss(None)


class GamePicker(ModalScreen[str | None]):
    """For loaded YAMLs that weight several games."""
    CSS = """
    GamePicker { align: center middle; }
    OptionList { width: 60%; height: auto; max-height: 80%; border: thick $primary; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, games: list[str]) -> None:
        super().__init__()
        self.games = games

    def compose(self) -> ComposeResult:
        yield OptionList(*[ListOption(game, id=game) for game in self.games])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class OptionsApp(MasterDetailApp):
    TITLE = "Options Creator"
    CSS = MASTER_DETAIL_CSS + """
    #top { height: 3; }
    #name { width: 30; }
    #game-label { padding: 1 2; }
    #random-opts { height: 3; }
    #random-opts Label { padding: 1 1; }
    #random-bias { width: 14; }
    #random-opts Input { width: 11; padding: 0 1; }
    """
    BINDINGS = [Binding("ctrl+g", "toggle_side", "Games"), Binding("ctrl+o", "load", "Load"),
                Binding("ctrl+s", "save", "Save"),
                Binding("ctrl+r", "reset", "Reset option")]

    def __init__(self, load_worlds: typing.Callable[[], typing.Any] | None = None) -> None:
        """`load_worlds` runs in a thread while the app shows; None if the worlds are already loaded."""
        super().__init__()
        self.load_worlds = load_worlds
        self.worlds: dict[str, type[World]] = {}
        self.game: str | None = None
        self.groups: dict[str, dict[str, type[Option]]] = {}
        self.options: dict[str, type[Option]] = {}
        self.values: dict[str, typing.Any] = {}  # the current game's entry in values_by_game
        self.values_by_game: dict[str, dict[str, typing.Any]] = {}  # switching games keeps each game's edits
        self.dirty = False  # unsaved changes
        self.loaded: dict = {}  # the YAML we loaded, kept so saving preserves everything we don't edit
        self.loaded_path: Path | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield Input(placeholder="Player name", id="name", max_length=MAX_NAME)
            yield Label("Game: (pick one)", id="game-label")
        with Horizontal():
            with Vertical(id="side"):
                yield Input(placeholder="Filter games", id="side-filter")
                yield OptionList(ListOption("Loading worlds…", disabled=True), id="side-list")
            yield OptionList(id="rows")
            with Vertical(id="detail"):
                yield Vertical(id="editor")
                with VerticalScroll(id="doc"):
                    yield Static(id="doc-text")
        yield Footer()

    def on_mount(self) -> None:
        prefs.bind_theme(self)
        if self.load_worlds:
            self.run_worker(self.load_in_thread, thread=True)
        else:
            self.worlds_loaded()
        self.query_one("#name", Input).focus()

    def load_in_thread(self) -> None:
        self.load_worlds()
        self.call_from_thread(self.worlds_loaded)

    def worlds_loaded(self) -> None:
        from worlds.AutoWorld import AutoWorldRegister
        self.worlds = {game: world for game, world in sorted(AutoWorldRegister.world_types.items())
                       if not world.hidden and world.web.options_page}
        self.fill_games()

    # games (side list)

    def fill_games(self) -> None:
        search = self.query_one("#side-filter", Input).value.strip().casefold()
        games = self.query_one("#side-list", OptionList)
        games.clear_options()
        games.add_options([ListOption(f"{game} (website)" if isinstance(world.web.options_page, str) else game, id=game)
                           for game, world in self.worlds.items() if search in game.casefold()])

    @on(Input.Changed, "#side-filter")
    def filter_games(self) -> None:
        self.fill_games()

    @on(Input.Submitted, "#side-filter")
    def pick_first_game(self) -> None:
        games = self.query_one("#side-list", OptionList)
        if games.option_count:
            self.select_game(games.get_option_at_index(0).id)

    @on(OptionList.OptionSelected, "#side-list")
    def game_selected(self, event: OptionList.OptionSelected) -> None:
        self.select_game(event.option.id)

    def select_game(self, game: str, values: dict | None = None) -> None:
        """Show a game's options; `values` (from a loaded file) replace any edits made to it so far."""
        world = self.worlds[game]
        if isinstance(world.web.options_page, str):
            self.notify(f"{game} configures its options on its website: {world.web.options_page}", timeout=10)
            return
        self.game = game
        self.groups = game_options(world)
        self.options = {key: option for group in self.groups.values() for key, option in group.items()}
        if values is not None or game not in self.values_by_game:
            self.values_by_game[game] = {key: default_value(option) for key, option in self.options.items()}
            for key, value in (values or {}).items():
                if key in self.options:
                    self.values_by_game[game][key] = normalize(self.options[key], value)
        self.values = self.values_by_game[game]
        self.query_one("#game-label", Label).update(f"Game: {game}")
        self.collapse_side_if_narrow()
        self.name_width = max((len(getattr(option, "display_name", key)) for key, option in self.options.items()),
                              default=10)
        rows = []
        for group, members in self.groups.items():
            rows.append(ListOption(Text(group, style="bold underline"), disabled=True))
            rows += [ListOption(self.row(key), id=key) for key in members]
        self.show_rows(rows)
        self.query_one("#rows", OptionList).focus()

    # rows

    def row_keys(self) -> typing.Iterable[str]:
        return self.options

    def row(self, key: str) -> Text:
        option = self.options[key]
        value = self.values[key]
        note = Text("  (weighted)", style="italic") if is_weighted(option, value) else None
        return self.fitted_row(getattr(option, "display_name", key), describe(value),
                               value != default_value(option), note)

    def set_value(self, key: str, value: typing.Any) -> None:
        if self.values.get(key) != value:
            self.dirty = True
        self.values[key] = value
        self.refresh_row(key)

    # editor for the highlighted option

    async def build_editor(self) -> None:
        key = self.editor_key
        option = self.options[key]
        value = self.values[key]
        option_kind = kind(option)
        widgets: list = [Label(Text(getattr(option, "display_name", key), style="bold")),
                         Label(Text(f"{option.__name__}  (default: {describe(default_value(option))})", style="dim"))]
        if can_randomize(option):
            widgets.append(Checkbox("Random", is_random(value), id="random"))
            if option_kind in RANGE_KINDS:
                bias, low, high = parse_random(value) if is_random(value) else ("", None, None)
                widgets.append(Horizontal(
                    Select([(label, name) for name, label in RANDOM_BIASES.items()], value=bias, allow_blank=False,
                           id="random-bias"),
                    Label("from"),
                    Input("" if low is None else str(low), type="integer", id="random-low",
                          placeholder=str(option.range_start)),
                    Label("to"),
                    Input("" if high is None else str(high), type="integer", id="random-high",
                          placeholder=str(option.range_end)),
                    id="random-opts"))

        if is_weighted(option, value) or not isinstance(value, (str, int, bool, list, dict)):
            widgets += self.yaml_editor(value, "Weighted values; edit as YAML.")
        elif option_kind in CHOICE_KINDS:
            names = list(option.name_lookup.values())
            widgets.append(OptionList(*[ListOption(name) for name in names], id="choice"))
            if option_kind == "textchoice":
                custom = value if isinstance(value, str) and value not in names and not is_random(value) else ""
                widgets.append(Input(custom, placeholder="Custom value (Enter to set)", id="custom"))
        elif option_kind in RANGE_KINDS:
            if option_kind == "named":
                widgets.append(OptionList(*[ListOption(f"{name} ({number})")
                                            for name, number in option.special_range_names.items()], id="special"))
            number = value if isinstance(value, int) else ""
            widgets.append(Input(str(number), type="integer", id="number",
                                 placeholder=f"{option.range_start}-{option.range_end} (Enter to set)"))
        elif option_kind == "text":
            widgets.append(Input(str(value), placeholder="Text (Enter to set)", id="text"))
        elif option_kind == "set":
            widgets.append(Input(placeholder=f"Filter {len(option.valid_keys)} entries", id="filter"))
            widgets.append(SelectionList[str](*self.selections(option, value, ""), id="selection"))
        elif option_kind == "list":
            widgets += self.yaml_editor(value, "A YAML list, one '- entry' per line.")
        elif option_kind == "counter":
            limits = f" ({option.min}-{option.max})" if option.min is not None or option.max is not None else ""
            widgets += self.yaml_editor(value, f"A YAML mapping, one 'name: count{limits}' per line.")
        else:
            widgets += self.yaml_editor(value, "Edit as YAML.")
        widgets.append(Label("", id="error"))
        editor = self.query_one("#editor")
        await editor.mount_all(widgets)
        self.sync_disabled()
        if option_kind in CHOICE_KINDS:
            names = list(option.name_lookup.values())
            if value in names:
                editor.query_one("#choice", OptionList).highlighted = names.index(value)

        self.query_one("#doc-text", Static).update(Text(cleandoc(option.__doc__ or "No description.")))
        self.query_one("#doc", VerticalScroll).scroll_home(animate=False)

    @staticmethod
    def yaml_editor(value: typing.Any, hint: str) -> list:
        return [Label(Text(hint, style="dim")), TextArea(to_yaml(value), id="yaml")]

    @staticmethod
    def selections(option: type[Option], value: typing.Any, search: str) -> list[tuple[str, str, bool]]:
        chosen = set(value) if isinstance(value, list) else set()
        return [(key, key, key in chosen) for key in sorted(option.valid_keys) if search in key.casefold()]

    def sync_disabled(self) -> None:
        """Random disables the other editor widgets and shows the random variants (range options)."""
        random = bool(self.query("#random")) and self.query_one("#random", Checkbox).value
        for widget in self.query_one("#editor").children:
            if widget.id == "random-opts":
                widget.display = random
            elif widget.id not in ("random", "error", None):
                widget.disabled = random

    @on(Checkbox.Changed, "#random")
    def random_changed(self, event: Checkbox.Changed) -> None:
        key = self.editor_key
        if event.value and not is_random(self.values[key]):
            self.set_value(key, "random")
            if self.query("#random-opts"):
                self.random_variant_changed()  # keep the variant still shown in the editor
        elif not event.value and is_random(self.values[key]):
            default = default_value(self.options[key])
            self.set_value(key, default if not is_random(default) else self.fallback(key))
        self.sync_disabled()

    @on(Select.Changed, "#random-bias")
    @on(Input.Submitted, "#random-low, #random-high")
    def random_variant_changed(self) -> None:
        option = self.options[self.editor_key]
        if not is_random(self.values[self.editor_key]):
            return
        bias = self.query_one("#random-bias", Select).value
        texts = [self.query_one(f"#random-{end}", Input).value.strip() for end in ("low", "high")]
        low = high = None
        if any(texts):
            try:
                low, high = sorted(int(text) if text else default
                                   for text, default in zip(texts, (option.range_start, option.range_end)))
            except ValueError:
                self.error("Not a number.")
                return
            if low < option.range_start or high > option.range_end:
                self.error(f"Must be between {option.range_start} and {option.range_end}.")
                return
            if low < 0:
                self.error("Archipelago can't read negative bounds in a random range.")
                return
        self.error("")
        self.set_value(self.editor_key, random_text(bias, low, high))

    def fallback(self, key: str) -> typing.Any:
        """A concrete value for options whose default is random."""
        option = self.options[key]
        if kind(option) in RANGE_KINDS:
            return option.range_start
        return next(iter(getattr(option, "name_lookup", {}).values()), "")

    @on(OptionList.OptionSelected, "#choice")
    def choice_selected(self, event: OptionList.OptionSelected) -> None:
        self.set_value(self.editor_key, list(self.options[self.editor_key].name_lookup.values())[event.option_index])
        self.error("")

    @on(OptionList.OptionSelected, "#special")
    def special_selected(self, event: OptionList.OptionSelected) -> None:
        self.set_value(self.editor_key, list(self.options[self.editor_key].special_range_names)[event.option_index])
        self.error("")

    @on(Input.Submitted, "#custom")
    def custom_submitted(self, event: Input.Submitted) -> None:
        if event.value.strip():
            self.set_value(self.editor_key, event.value.strip())

    @on(Input.Submitted, "#text")
    def text_submitted(self, event: Input.Submitted) -> None:
        self.set_value(self.editor_key, event.value)

    @on(Input.Submitted, "#number")
    def number_submitted(self, event: Input.Submitted) -> None:
        option = self.options[self.editor_key]
        try:
            number = int(event.value)
        except ValueError:
            self.error("Not a number.")
            return
        if not option.range_start <= number <= option.range_end:
            self.error(f"Must be between {option.range_start} and {option.range_end}.")
            return
        self.error("")
        self.set_value(self.editor_key, number)

    @on(Input.Changed, "#filter")
    def filter_changed(self, event: Input.Changed) -> None:
        option = self.options[self.editor_key]
        selection = self.query_one("#selection", SelectionList)
        selection.clear_options()
        selection.add_options(self.selections(option, self.values[self.editor_key], event.value.strip().casefold()))

    @on(SelectionList.SelectionToggled, "#selection")
    def selection_toggled(self, event: SelectionList.SelectionToggled) -> None:
        chosen = list(self.values[self.editor_key]) if isinstance(self.values[self.editor_key], list) else []
        item = event.selection.value
        if item in chosen:
            chosen.remove(item)
        else:
            chosen.append(item)
        self.set_value(self.editor_key, sorted(chosen))

    @on(TextArea.Changed, "#yaml")
    def yaml_changed(self, event: TextArea.Changed) -> None:
        option = self.options[self.editor_key]
        option_kind = kind(option)
        ok, value = self.parse_yaml(event.text_area.text)
        if not ok:
            return
        if value is None:
            value = [] if option_kind in LIST_KINDS else {} if option_kind in MAPPING_KINDS else ""
        if option_kind in LIST_KINDS and not isinstance(value, list):
            self.error("Expected a list (one '- entry' per line).")
            return
        if option_kind in MAPPING_KINDS and not isinstance(value, dict):
            self.error("Expected a mapping (one 'name: count' per line).")
            return
        valid = getattr(option, "valid_keys", None)
        if valid and isinstance(value, (list, dict)):
            unknown = [k for k in value if k not in valid]
            if unknown:
                self.error(f"Unknown: {', '.join(map(str, unknown[:5]))}")
                return
        self.set_value(self.editor_key, value)

    async def action_reset(self) -> None:
        if self.editor_key:
            self.set_value(self.editor_key, default_value(self.options[self.editor_key]))
            await self._build_editor_now()

    # load / save

    def action_load(self) -> None:
        if self.dirty and not self.confirm_twice(
                "load", "Unsaved changes. Loading a file replaces this game's edits: press Ctrl+O again to load "
                        "anyway, or Ctrl+S to save first."):
            return
        self.push_screen(FilePicker("Load options", [".yaml", ".yml"], "options_dir", DEFAULT_DIR), self.load_file)

    def load_file(self, path: Path | None) -> None:
        if path is None:
            return
        try:
            with open(path, encoding="utf-8-sig") as f:
                documents = [doc for doc in Utils.parse_yamls(f) if doc]
        except Exception as e:
            self.notify(f"Could not read {path.name}: {e}", severity="error", timeout=10)
            return
        if not documents:
            self.notify(f"{path.name} is empty", severity="error")
            return
        if len(documents) > 1:
            self.notify(f"{path.name} has {len(documents)} players; editing the first", timeout=8)
        data = documents[0]
        game = data.get("game")
        if isinstance(game, dict):
            games = [g for g, weight in game.items() if weight and g in self.worlds]
            if len(games) == 1:
                game = games[0]
            elif games:
                self.push_screen(GamePicker(games), lambda chosen: chosen and self.apply_loaded(path, data, chosen))
                return
            else:
                game = None
        if game not in self.worlds:
            self.notify(f"{path.name}: game {game!r} isn't installed", severity="error", timeout=10)
            return
        self.apply_loaded(path, data, game)

    def apply_loaded(self, path: Path, data: dict, game: str) -> None:
        self.loaded, self.loaded_path = data, path
        self.query_one("#name", Input).value = str(data.get("name", ""))[:MAX_NAME]
        section = data.get(game) if isinstance(data.get(game), dict) else {}
        self.select_game(game, section)
        self.dirty = False
        self.notify(f"Loaded {path.name}")

    def action_save(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self.notify("Enter a player name first", severity="error")
            self.query_one("#name", Input).focus()
            return
        if not self.game:
            self.notify("Pick a game first", severity="error")
            return
        folder = prefs.get("options_dir") or DEFAULT_DIR
        default = str(self.loaded_path) if self.loaded_path else \
            os.path.join(folder, f"{Utils.get_file_safe_name(name)}.yaml")
        self.push_screen(SaveScreen(default), self.save_file)

    def save_file(self, path: Path | None) -> None:
        if path is None:
            return
        data = dict(self.loaded)
        data["name"] = self.query_one("#name", Input).value.strip()
        data.setdefault("description", f"YAML generated by Archipelago {Utils.__version__} (aplite).")
        if not isinstance(data.get("game"), dict):  # keep a loaded multi-game weighting as it was
            data["game"] = self.game
        section = dict(data.get(self.game) or {}) if isinstance(data.get(self.game), dict) else {}
        section.update(self.values)
        data[self.game] = section
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(Utils.dump(data, sort_keys=False))
        except OSError as e:
            self.notify(f"Could not save: {e}", severity="error", timeout=10)
            return
        prefs.put("options_dir", str(path.parent))
        self.loaded, self.loaded_path = data, path
        self.dirty = False
        self.notify(f"Saved {path}")


def run(load_worlds: typing.Callable[[], typing.Any] | None = None) -> None:
    OptionsApp(load_worlds).run()
