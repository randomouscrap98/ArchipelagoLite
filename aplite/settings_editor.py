"""
host.yaml editor (see master_detail.py for the layout). Edits are kept here until saved, then applied with upstream's
Group.update (which converts to the setting types) and written by Settings.save, so host.yaml keeps its comments.
Raw values are read directly: normal attribute access on a missing required path would prompt for it.
"""
from __future__ import annotations

import dataclasses
import enum
import os
import shlex
import subprocess
import types
import typing
from inspect import cleandoc
from shutil import which

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Checkbox, Footer, Input, Label, OptionList, TextArea
from textual.widgets.option_list import Option as ListOption

import settings
from aplite.combo import keep_scroll
from aplite.master_detail import SIDE_TEXT_WIDTH, EditorApp, MasterDetailScreen, Rows, cycle, describe, detail_pane, \
    editor_event, fit, heading, side_pane, to_yaml, yaml_editor

PATH_KINDS = frozenset({"file", "folder"})


def kind(hint: typing.Any) -> tuple[str, type | None]:
    """Editor kind for a setting's type hint, and the class that decides it."""
    union = typing.get_origin(hint) in (typing.Union, types.UnionType)
    args = [a for a in typing.get_args(hint) if a is not type(None)] if union else [hint]
    if any(isinstance(a, type) and issubclass(a, settings.Bool) for a in args) or bool in args:
        return "bool", next((a for a in args if isinstance(a, type) and issubclass(a, settings.Bool)), None)
    cls = args[0] if args and isinstance(args[0], type) else None
    if cls is None:
        return "yaml", None
    if issubclass(cls, settings.FolderPath):
        return "folder", cls
    if issubclass(cls, settings.Path):
        return "file", cls
    if issubclass(cls, enum.IntEnum):
        return "enum", cls
    if issubclass(cls, int):
        return "int", cls
    if issubclass(cls, str):
        return "str", cls
    return "yaml", cls


def raw(group: settings.Group, name: str) -> typing.Any:
    """A setting's value as stored: getattr would resolve (and validate) paths, "" resolves to the AP dir, and a
    missing required path would prompt for it."""
    return group.__dict__[name] if name in group.__dict__ else getattr(type(group), name, None)


def plain(value: typing.Any) -> typing.Any:
    """Setting value -> builtin value for editing (IntEnum -> int, Path -> str, Group -> dict, list subclass -> list)."""
    if isinstance(value, settings.Group):
        return {k: plain(raw(value, k)) for k in value}
    value = settings._to_builtin(value)  # also list/dict subclasses (sc2 colors): YAML can only show builtins
    if isinstance(value, (list, tuple, set)):
        return [plain(item) for item in value]
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    return value


@dataclasses.dataclass
class Setting:
    kind: str  # see kind()
    cls: type | None  # the setting class (Path subclass, IntEnum, ...) when there is one
    value: typing.Any  # builtin value being edited
    default: typing.Any
    doc: str
    required: bool  # paths: upstream asks for it when missing

    def path_exists(self) -> bool:
        return bool(self.value) and self.cls(self.value).exists()

    def missing_path(self) -> bool:
        return self.kind in PATH_KINDS and bool(self.value) and not self.path_exists()


class Section:
    """A host.yaml section: a Group with editable settings, or a dict left over from a world that isn't installed."""

    def __init__(self, key: str, group: settings.Group | dict) -> None:
        self.key = key
        self.group = group
        self.editable = isinstance(group, settings.Group)
        self.settings: dict[str, Setting] = {}
        if not self.editable:
            self.title = f"{key} (not installed)"
            self.doc = "This section is in host.yaml but no installed world uses it. Shown read-only."
            return
        cls = type(group)
        hints = cls.get_type_hints()
        self.title = key.replace("_", " ").title()
        self.doc = cleandoc(cls.__doc__) if cls.__doc__ else ""
        for name in group:
            value = raw(group, name)
            editor, setting_cls = kind(hints.get(name, type(value)))
            doc_cls = setting_cls or (type(value) if type(value).__module__ != "builtins" else None)
            self.settings[name] = Setting(
                kind=editor, cls=setting_cls, value=plain(value), default=plain(getattr(cls, name, None)),
                doc=cleandoc(doc_cls.__doc__) if doc_cls is not None and doc_cls.__doc__ else "",
                required=getattr(setting_cls, "required", False))


class SettingsScreen(MasterDetailScreen):
    BINDINGS = [Binding("ctrl+g", "toggle_side", "Sections"), Binding("ctrl+s", "save", "Save"),
                Binding("ctrl+e", "external", "Edit in $EDITOR"), Binding("ctrl+r", "reset", "Reset setting"),
                Binding("ctrl+b", "browse", "Browse")]
    NARROW = 100  # sections get browsed more than games, so keep the list longer
    MISSING_NOTE = " (saving is still allowed)"

    def __init__(self) -> None:
        super().__init__()
        self.sections: dict[str, Section] = {}
        self.section: Section | None = None
        self.changed: dict[str, set[str]] = {}  # section key -> setting names edited since the last save

    @property
    def dirty(self) -> bool:
        return any(self.changed.values())

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield side_pane("Filter sections")
            yield Rows(id="rows")
            yield detail_pane()
        yield Footer()

    def on_mount(self) -> None:
        self.load()
        if self.sections:
            self.select_section(next(iter(self.sections)), focus=False)
        self.query_one("#side-filter", Input).focus()

    def load(self) -> None:
        current = settings.get_settings()
        settings._update_cache()
        names = list(current)
        names += [name for name in settings._world_settings_name_cache if name not in names]
        self.sections = {}
        for name in names:
            group = raw(current, name)
            if name in settings._world_settings_name_cache or isinstance(group, settings.Group):
                group = getattr(current, name)  # upcasts an installed world's dict to its Group
            self.sections[name] = Section(name, group if group is not None else {})
        self.sub_title = current.filename or ""
        self.fill_side()

    # sections (side list)

    def side_entries(self, search: str) -> list[ListOption]:
        return [ListOption(Text(fit(s.title, SIDE_TEXT_WIDTH), style="" if s.editable else "dim"), id=key)
                for key, s in self.sections.items() if search in s.title.casefold() or search in key]

    def open_side(self, key: str) -> None:
        self.select_section(key)

    @on(OptionList.OptionHighlighted, "#side-list")
    def section_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.select_section(event.option.id, focus=False)

    def select_section(self, key: str, focus: bool = True) -> None:
        if not (self.section and self.section.key == key):
            self.section = self.sections[key]
            self.name_width = max((len(name) for name in self.section.settings), default=10)
            if self.section.editable:
                self.show_rows([ListOption(self.row(name), id=name) for name in self.section.settings])
            else:
                self.show_rows([ListOption(Text("(read-only, see the right pane)", style="dim"), disabled=True)])
                self.show_readonly()
        if focus:
            self.collapse_side_if_narrow()
            self.query_one("#rows", OptionList).focus()

    def show_readonly(self) -> None:
        self.query_one("#editor").remove_children()
        text = to_yaml(self.section.group) or "{}"
        self.show_doc(self.section.doc + "\n\n" + text)

    # rows

    def row_keys(self) -> typing.Iterable[str]:
        return self.section.settings if self.section else ()

    def row(self, name: str) -> Text:
        setting = self.section.settings[name]
        value = describe(setting.value) if setting.value not in (None, "") else "(not set)"
        note = Text("  missing", style="red") if setting.missing_path() else None
        return self.fitted_row(name, value, setting.value != setting.default, note)

    def stepped(self, name: str, delta: int) -> typing.Any:
        if not self.section or not self.section.editable:
            return None
        setting = self.section.settings[name]
        if setting.kind == "bool":
            return not setting.value
        if setting.kind == "enum":
            return cycle([m.value for m in setting.cls], setting.value, delta)
        if setting.kind == "int":
            return (setting.value if isinstance(setting.value, int) else 0) + delta
        return None

    def update_editor(self, name: str) -> bool:
        setting = self.section.settings[name]
        if self.query("#bool"):
            self.query_one("#bool", Checkbox).value = bool(setting.value)
        elif self.query("#enum") and setting.value in (values := [m.value for m in setting.cls]):
            self.query_one("#enum", OptionList).highlighted = values.index(setting.value)
        elif self.query("#int"):
            self.query_one("#int", Input).value = str(setting.value)
        else:
            return False
        return True

    def set_value(self, name: str, value: typing.Any) -> None:
        setting = self.section.settings[name]
        if setting.value != value:
            setting.value = value
            self.changed.setdefault(self.section.key, set()).add(name)
        self.refresh_row(name)

    # editor for the highlighted setting

    def default_of(self, name: str) -> typing.Any:
        return self.section.settings[name].default

    def setting(self) -> Setting:
        return self.section.settings[self.editor_key]

    async def build_editor(self) -> None:
        if not self.section or not self.section.editable:
            return
        setting = self.setting()
        value = setting.value
        type_name = setting.cls.__name__ if setting.cls else setting.kind
        widgets: list = heading(self.editor_key, f"{type_name}  (default: {describe(setting.default)})",
                                end=setting.kind not in PATH_KINDS)
        if setting.kind in PATH_KINDS:
            widgets.append(Label(Text("required" if setting.required else "optional", style="dim"),
                                 classes="heading-end"))
            widgets += self.path_editor(str(value or ""))
        elif setting.kind == "bool":
            widgets.append(Checkbox("Enabled", bool(value), id="bool"))
        elif setting.kind == "enum":
            widgets.append(OptionList(*[ListOption(f"{m.name} ({m.value})") for m in setting.cls], id="enum"))
        elif setting.kind == "int":
            widgets.append(Input(str(value if value is not None else ""), type="integer", id="int",
                                 placeholder="Number (Enter to set)"))
        elif setting.kind == "str":
            widgets.append(Input(str(value if value is not None else ""), id="str", placeholder="Text (Enter to set)"))
        else:
            widgets += yaml_editor(value, "Edit as YAML.")
        widgets.append(Label("", id="error"))
        await self.mount_editor(widgets)
        editor = self.query_one("#editor")
        if setting.kind == "enum":
            values = [m.value for m in setting.cls]
            if value in values:
                editor.query_one("#enum", OptionList).highlighted = values.index(value)
        self.update_exists()
        self.show_doc(setting.doc or "No description.")

    def resolved_path(self, name: str) -> str:
        setting = self.section.settings[name]
        return str(setting.cls(setting.value).resolve()) if setting.value else ""

    def path_note(self, name: str) -> str:
        return " (will be asked for when needed)" if self.section.settings[name].required else ""

    def picks_folder(self, name: str) -> bool:
        return self.section.settings[name].kind == "folder"

    @on(Checkbox.Changed, "#bool")
    @editor_event
    def bool_changed(self, event: Checkbox.Changed) -> None:
        self.set_value(self.editor_key, event.value)

    @on(OptionList.OptionSelected, "#enum")
    @editor_event
    def enum_selected(self, event: OptionList.OptionSelected) -> None:
        self.set_value(self.editor_key, list(self.setting().cls)[event.option_index].value)
        self.back_to_rows()

    @on(TextArea.Changed, "#yaml")
    @editor_event
    def yaml_changed(self, event: TextArea.Changed) -> None:
        ok, value = self.parse_yaml(event.text_area.text)
        if ok:
            self.set_value(self.editor_key, value)

    # save / external editor / quit

    def action_save(self) -> None:
        if not self.dirty:
            self.notify("No changes to save")
            return
        current = settings.get_settings()
        missing = []
        for key, names in self.changed.items():
            section = self.sections[key]
            section.group.update({name: section.settings[name].value for name in names})
            missing += [f"{key}.{name}" for name in names if section.settings[name].missing_path()]
        try:
            current.save()
        except Exception as e:
            self.notify(f"Could not save: {e}", severity="error", timeout=10)
            return
        self.changed.clear()
        message = f"Saved {current.filename}"
        if missing:
            message += f". {len(missing)} path(s) don't exist: {', '.join(missing[:3])}"
        self.notify(message, severity="warning" if missing else "information", timeout=8)

    def action_external(self) -> None:
        if self.dirty:
            self.notify("Save first (Ctrl+S): the file is reloaded after the external editor closes.",
                        severity="warning", timeout=6)
            return
        current = settings.get_settings()
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or \
            next((e for e in ("sensible-editor", "nano", "vim", "vi") if which(e)), None)
        if not editor or not current.filename:
            self.notify("No editor found; set $EDITOR", severity="error")
            return
        with self.app.suspend():
            subprocess.call([*shlex.split(editor), current.filename])
        settings.get_settings._cache = None  # reload what the editor wrote
        side, rows = self.query_one("#side-list", OptionList), self.query_one("#rows", OptionList)
        selected = self.section.key if self.section else None
        row = rows.highlighted_option.id if rows.highlighted_option else None
        keep_scroll(side)
        keep_scroll(rows)
        self.section = None
        with self.prevent(OptionList.OptionHighlighted):  # the refilled side list would select its first section
            self.load()
            ids = [option.id for option in side.options]
            if selected in ids:
                side.highlighted = ids.index(selected)
        if selected in self.sections:
            self.select_section(selected, focus=False)
            ids = [option.id for option in rows.options]
            if row in ids:
                rows.highlighted = ids.index(row)
        self.notify("Reloaded host.yaml")


def run() -> None:
    EditorApp(SettingsScreen(), "Settings (host.yaml)").run()
