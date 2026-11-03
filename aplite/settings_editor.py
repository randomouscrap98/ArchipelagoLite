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
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Checkbox, Footer, Input, Label, OptionList, Static, TextArea
from textual.widgets.option_list import Option as ListOption

import settings
from aplite.combo import FilterInput, highlight_first
from aplite.file_picker import FilePicker, PathInput
from aplite.master_detail import CSS as MASTER_DETAIL_CSS, SIDE_TEXT_WIDTH, MasterDetailApp, Rows, describe, \
    editor_event, fit, to_yaml

if typing.TYPE_CHECKING:
    from pathlib import Path

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


def plain(value: typing.Any) -> typing.Any:
    """Setting value -> builtin value for editing (IntEnum -> int, Path -> str, Group -> dict, list subclass -> list)."""
    if isinstance(value, settings.Group):
        # raw values: getattr would resolve (and validate) paths, and "" resolves to the AP dir
        return {k: plain(value.__dict__[k] if k in value.__dict__ else getattr(type(value), k, None))
                for k in value}
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
            raw = group.__dict__[name] if name in group.__dict__ else getattr(cls, name, None)
            editor, setting_cls = kind(hints.get(name, type(raw)))
            doc_cls = setting_cls or (type(raw) if type(raw).__module__ != "builtins" else None)
            self.settings[name] = Setting(
                kind=editor, cls=setting_cls, value=plain(raw), default=plain(getattr(cls, name, None)),
                doc=cleandoc(doc_cls.__doc__) if doc_cls is not None and doc_cls.__doc__ else "",
                required=getattr(setting_cls, "required", False))


class SettingsApp(MasterDetailApp):
    TITLE = "Settings (host.yaml)"
    CSS = MASTER_DETAIL_CSS
    BINDINGS = [Binding("ctrl+g", "toggle_side", "Sections"), Binding("ctrl+s", "save", "Save"),
                Binding("ctrl+e", "external", "Edit in $EDITOR"), Binding("ctrl+r", "reset", "Reset setting"),
                Binding("ctrl+b", "browse", "Browse", show=False)]

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
            with Vertical(id="side"):
                yield FilterInput(placeholder="Filter sections", id="side-filter", target="#side-list")
                yield OptionList(id="side-list")
            yield Rows(id="rows")
            with Vertical(id="detail"):
                yield Vertical(id="editor")
                with VerticalScroll(id="doc"):
                    yield Static(id="doc-text")
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
            raw = current.__dict__.get(name, getattr(type(current), name, None))
            if name in settings._world_settings_name_cache or isinstance(raw, settings.Group):
                raw = getattr(current, name)  # upcasts an installed world's dict to its Group
            self.sections[name] = Section(name, raw if raw is not None else {})
        self.sub_title = current.filename or ""
        self.fill_sections()

    # sections (side list)

    def fill_sections(self) -> None:
        search = self.query_one("#side-filter", Input).value.strip().casefold()
        sections = self.query_one("#side-list", OptionList)
        sections.clear_options()
        sections.add_options([ListOption(Text(fit(s.title, SIDE_TEXT_WIDTH), style="" if s.editable else "dim"), id=key)
                              for key, s in self.sections.items()
                              if search in s.title.casefold() or search in key])
        highlight_first(sections)

    @on(Input.Changed, "#side-filter")
    def filter_sections(self) -> None:
        self.fill_sections()

    @on(Input.Submitted, "#side-filter")
    def pick_highlighted(self) -> None:
        option = self.query_one("#side-list", OptionList).highlighted_option
        if option:
            self.select_section(option.id)

    @on(OptionList.OptionSelected, "#side-list")
    def section_selected(self, event: OptionList.OptionSelected) -> None:
        self.select_section(event.option.id)

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
        self.query_one("#doc-text", Static).update(Text(self.section.doc + "\n\n" + text))

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
            values = [m.value for m in setting.cls]
            index = values.index(setting.value) if setting.value in values else (-1 if delta > 0 else 0)
            return values[(index + (1 if delta > 0 else -1)) % len(values)]
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

    def setting(self) -> Setting:
        return self.section.settings[self.editor_key]

    async def build_editor(self) -> None:
        if not self.section or not self.section.editable:
            return
        setting = self.setting()
        value = setting.value
        type_name = setting.cls.__name__ if setting.cls else setting.kind
        widgets: list = [Label(Text(self.editor_key, style="bold")),
                         Label(Text(f"{type_name}  (default: {describe(setting.default)})", style="dim"),
                               classes="" if setting.kind in PATH_KINDS else "heading-end")]
        if setting.kind in PATH_KINDS:
            widgets.append(Label(Text("required" if setting.required else "optional", style="dim"),
                                 classes="heading-end"))
            widgets.append(PathInput(str(value or ""), id="path",
                                     placeholder="Path. Enter sets, Tab completes, Ctrl+B browses"))
            widgets.append(Label("", id="exists"))
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
            widgets += [Label(Text("Edit as YAML.", style="dim")), TextArea(to_yaml(value), id="yaml")]
        widgets.append(Label("", id="error"))
        await self.mount_editor(widgets)
        editor = self.query_one("#editor")
        if setting.kind == "enum":
            values = [m.value for m in setting.cls]
            if value in values:
                editor.query_one("#enum", OptionList).highlighted = values.index(value)
        self.update_exists()
        self.query_one("#doc-text", Static).update(Text(setting.doc or "No description."))
        self.query_one("#doc", VerticalScroll).scroll_home(animate=False)

    def update_exists(self) -> None:
        if not self.query("#exists"):
            return
        setting = self.setting()
        if not setting.value:
            text = Text("not set" + (" (will be asked for when needed)" if setting.required else ""), style="dim")
        elif setting.path_exists():
            text = Text("√ exists", style="green")
        else:
            text = Text("x doesn't exist (saving is still allowed)", style="red")
        self.query_one("#exists", Label).update(text)

    @on(Input.Submitted, "#path")
    @editor_event
    def path_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.set_value(self.editor_key, os.path.expanduser(event.value.strip()))
        self.update_exists()
        self.back_to_rows()

    def action_browse(self) -> None:
        if not self.query("#path"):
            return
        setting = self.setting()
        start = os.path.dirname(setting.cls(setting.value).resolve()) if setting.value else os.path.expanduser("~")

        def chosen(path: Path | None) -> None:
            if path is not None:
                self.query_one("#path", PathInput).value = str(path)
                self.set_value(self.editor_key, str(path))
                self.update_exists()

        self.push_screen(FilePicker(f"Choose {self.editor_key}", [], "settings_browse_dir",
                                    start if os.path.isdir(start) else os.path.expanduser("~"),
                                    folders=setting.kind == "folder", show_all=True), chosen)

    @on(Checkbox.Changed, "#bool")
    @editor_event
    def bool_changed(self, event: Checkbox.Changed) -> None:
        self.set_value(self.editor_key, event.value)

    @on(OptionList.OptionSelected, "#enum")
    @editor_event
    def enum_selected(self, event: OptionList.OptionSelected) -> None:
        self.set_value(self.editor_key, list(self.setting().cls)[event.option_index].value)
        self.back_to_rows()

    @on(Input.Submitted, "#int")
    @editor_event
    def int_submitted(self, event: Input.Submitted) -> None:
        try:
            self.set_value(self.editor_key, int(event.value))
            self.error("")
            self.back_to_rows()
        except ValueError:
            self.error("Not a number.")

    @on(Input.Submitted, "#str")
    @editor_event
    def str_submitted(self, event: Input.Submitted) -> None:
        self.set_value(self.editor_key, event.value)
        self.back_to_rows()

    @on(TextArea.Changed, "#yaml")
    @editor_event
    def yaml_changed(self, event: TextArea.Changed) -> None:
        ok, value = self.parse_yaml(event.text_area.text)
        if ok:
            self.set_value(self.editor_key, value)

    async def action_reset(self) -> None:
        if self.editor_key and self.section and self.section.editable:
            self.set_value(self.editor_key, self.setting().default)
            await self._build_editor_now()

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
        with self.suspend():
            subprocess.call([*shlex.split(editor), current.filename])
        settings.get_settings._cache = None  # reload what the editor wrote
        selected = self.section.key if self.section else None
        self.section = None
        self.load()
        if selected in self.sections:
            self.select_section(selected, focus=False)
        self.notify("Reloaded host.yaml")


def run() -> None:
    SettingsApp().run()
