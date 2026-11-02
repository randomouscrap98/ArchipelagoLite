"""
host.yaml editor, same master-detail layout as the Options Creator. Edits are kept here until saved, then applied with
upstream's Group.update (which converts to the setting types) and written by Settings.save, so host.yaml keeps its
comments. Raw values are read directly: normal attribute access on a missing required path would prompt for it.
"""
from __future__ import annotations

import enum
import os
import shlex
import subprocess
import types
import typing
from inspect import cleandoc
from shutil import which

import yaml
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Checkbox, Footer, Input, Label, OptionList, Static, TextArea
from textual.widgets.option_list import Option as ListOption

import settings
from aplite import prefs
from aplite.file_picker import FilePicker, PathInput
from aplite.options_creator import EDITOR_DELAY, NARROW, describe, fit

if typing.TYPE_CHECKING:
    from pathlib import Path


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
    """Setting value -> builtin value for editing (IntEnum -> int, Path -> str, Group -> dict)."""
    if isinstance(value, settings.Group):
        return value.as_dict()
    if isinstance(value, (list, dict, tuple, set)) or value is None:
        return list(value) if isinstance(value, (tuple, set)) else value
    return settings._to_builtin(value)


class Section:
    def __init__(self, key: str, group: settings.Group | dict) -> None:
        self.key = key
        self.group = group
        self.editable = isinstance(group, settings.Group)
        if self.editable:
            cls = type(group)
            hints = cls.get_type_hints()
            self.title = key.replace("_", " ").title()
            self.doc = cleandoc(cls.__doc__) if cls.__doc__ else ""
            self.entries: dict[str, dict] = {}
            for name in group:
                raw = group.__dict__[name] if name in group.__dict__ else getattr(cls, name, None)
                editor, setting_cls = kind(hints.get(name, type(raw)))
                doc_cls = setting_cls or (type(raw) if type(raw).__module__ != "builtins" else None)
                self.entries[name] = {
                    "kind": editor, "cls": setting_cls, "value": plain(raw),
                    "default": plain(getattr(cls, name, None)),
                    "doc": cleandoc(doc_cls.__doc__) if doc_cls is not None and doc_cls.__doc__ else "",
                    "required": getattr(setting_cls, "required", False),
                }
        else:
            self.title = f"{key} (not installed)"
            self.doc = "This section is in host.yaml but no installed world uses it. Shown read-only."
            self.entries = {}


def path_exists(entry: dict) -> bool:
    if not entry["value"]:
        return False
    return entry["cls"](entry["value"]).exists()


class SettingsApp(App[None]):
    TITLE = "Settings (host.yaml)"
    CSS = """
    #sections-pane { width: 30; }
    #rows { width: 3fr; }
    #detail { width: 2fr; }
    #editor { height: auto; max-height: 60%; }
    #editor OptionList { height: auto; max-height: 15; }
    #editor TextArea { height: 12; }
    #doc { height: 1fr; border-top: solid $panel; padding: 0 1; }
    #error { color: $error; }
    """
    BINDINGS = [Binding("ctrl+g", "toggle_sections", "Sections"), Binding("ctrl+s", "save", "Save"),
                Binding("ctrl+e", "external", "Edit in $EDITOR"), Binding("ctrl+r", "reset", "Reset setting"),
                Binding("ctrl+b", "browse", "Browse", show=False),
                Binding("ctrl+q", "quit", "Quit")]

    def __init__(self) -> None:
        super().__init__()
        self.sections: dict[str, Section] = {}
        self.section: Section | None = None
        self.current: str | None = None
        self.editor_key: str | None = None
        self.rebuild_timer = None
        self.changed: dict[str, set[str]] = {}  # section -> setting names edited since the last save
        self.quit_armed = False
        self.name_width = 10

    @property
    def dirty(self) -> bool:
        return any(self.changed.values())

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="sections-pane"):
                yield Input(placeholder="Filter sections", id="section-filter")
                yield OptionList(id="sections")
            yield OptionList(id="rows")
            with Vertical(id="detail"):
                yield Vertical(id="editor")
                with VerticalScroll(id="doc"):
                    yield Static(id="doc-text")
        yield Footer()

    def on_mount(self) -> None:
        prefs.bind_theme(self)
        self.load()
        if self.sections:
            self.select_section(next(iter(self.sections)), focus=False)
        self.query_one("#section-filter", Input).focus()

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

    # sections

    def fill_sections(self) -> None:
        search = self.query_one("#section-filter", Input).value.strip().casefold()
        sections = self.query_one("#sections", OptionList)
        sections.clear_options()
        sections.add_options([ListOption(Text(fit(s.title, 26), style="" if s.editable else "dim"), id=key)
                              for key, s in self.sections.items()
                              if search in s.title.casefold() or search in key])

    @on(Input.Changed, "#section-filter")
    def filter_sections(self) -> None:
        self.fill_sections()

    @on(Input.Submitted, "#section-filter")
    def pick_first(self) -> None:
        sections = self.query_one("#sections", OptionList)
        if sections.option_count:
            self.select_section(sections.get_option_at_index(0).id)

    @on(OptionList.OptionSelected, "#sections")
    def section_selected(self, event: OptionList.OptionSelected) -> None:
        self.select_section(event.option.id)

    @on(OptionList.OptionHighlighted, "#sections")
    def section_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.select_section(event.option.id, focus=False)

    def select_section(self, key: str, focus: bool = True) -> None:
        if self.section and self.section.key == key:
            if focus:
                self.query_one("#rows", OptionList).focus()
            return
        self.section = self.sections[key]
        self.name_width = max((len(name) for name in self.section.entries), default=10)
        self.current = None
        rows = self.query_one("#rows", OptionList)
        rows.clear_options()
        if self.section.editable:
            rows.add_options([ListOption(self.row(name), id=name) for name in self.section.entries])
            if rows.option_count:
                rows.highlighted = 0
        else:
            rows.add_options([ListOption(Text("(read-only, see the right pane)", style="dim"), disabled=True)])
            self.show_readonly()
        if focus:
            if self.size.width < NARROW and self.query_one("#sections-pane").display:
                self.query_one("#sections-pane").display = False
                self.call_after_refresh(self.refresh_rows)
            rows.focus()

    def show_readonly(self) -> None:
        editor = self.query_one("#editor", Vertical)
        editor.remove_children()
        text = yaml.safe_dump(self.section.group, sort_keys=False, allow_unicode=True) if self.section.group else "{}"
        self.query_one("#doc-text", Static).update(Text(self.section.doc + "\n\n" + text))

    # rows

    def row(self, name: str) -> Text:
        entry = self.section.entries[name]
        width = max(30, self.query_one("#rows", OptionList).scrollable_content_region.width - 1)
        name_width = min(self.name_width, width - 16)
        changed = entry["value"] != entry["default"]
        text = Text("  " + fit(name, name_width).ljust(name_width + 2))
        value = describe(entry["value"]) if entry["value"] not in (None, "") else "(not set)"
        text.append(fit(value, width - name_width - 6), style="bold" if changed else "dim")
        if entry["kind"] in ("file", "folder") and entry["value"] and not path_exists(entry):
            text.append("  missing", style="red")
        return text

    def refresh_row(self, name: str) -> None:
        self.query_one("#rows", OptionList).replace_option_prompt(name, self.row(name))

    def refresh_rows(self) -> None:
        if self.section and self.section.editable:
            for name in self.section.entries:
                self.refresh_row(name)

    def on_resize(self) -> None:
        self.call_after_refresh(self.refresh_rows)

    def action_toggle_sections(self) -> None:
        pane = self.query_one("#sections-pane")
        pane.display = not pane.display
        (self.query_one("#section-filter", Input) if pane.display else self.query_one("#rows", OptionList)).focus()
        self.call_after_refresh(self.refresh_rows)

    def set_value(self, name: str, value: typing.Any) -> None:
        entry = self.section.entries[name]
        if entry["value"] != value:
            entry["value"] = value
            self.changed.setdefault(self.section.key, set()).add(name)
        self.refresh_row(name)

    @on(OptionList.OptionHighlighted, "#rows")
    def row_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id and event.option.id != self.current:
            self.current = event.option.id
            if self.rebuild_timer:
                self.rebuild_timer.stop()
            self.rebuild_timer = self.set_timer(EDITOR_DELAY, self.build_editor)

    # editor

    async def build_editor(self) -> None:
        self.rebuild_timer = None
        editor = self.query_one("#editor", Vertical)
        await editor.remove_children()
        name = self.editor_key = self.current
        if not name or not self.section or not self.section.editable:
            return
        entry = self.section.entries[name]
        value = entry["value"]
        widgets: list = [Label(Text(name, style="bold"))]
        detail = f"{entry['cls'].__name__ if entry['cls'] else entry['kind']}  (default: {describe(entry['default'])})"
        widgets.append(Label(Text(detail, style="dim")))
        if entry["kind"] in ("file", "folder"):
            widgets.append(Label(Text("required" if entry["required"] else "optional", style="dim")))
            widgets.append(PathInput(str(value or ""), id="path",
                                     placeholder="Path. Enter sets, Tab completes, Ctrl+B browses"))
            widgets.append(Label("", id="exists"))
        elif entry["kind"] == "bool":
            widgets.append(Checkbox("Enabled", bool(value), id="bool"))
        elif entry["kind"] == "enum":
            members = list(entry["cls"])
            widgets.append(OptionList(*[ListOption(f"{m.name} ({m.value})") for m in members], id="enum"))
        elif entry["kind"] == "int":
            widgets.append(Input(str(value if value is not None else ""), type="integer", id="int",
                                 placeholder="Number (Enter to set)"))
        elif entry["kind"] == "str":
            widgets.append(Input(str(value if value is not None else ""), id="str", placeholder="Text (Enter to set)"))
        else:
            text = yaml.safe_dump(value, sort_keys=False, allow_unicode=True) if value not in (None, "") else ""
            widgets += [Label(Text("Edit as YAML.", style="dim")), TextArea(text, id="yaml")]
        widgets.append(Label("", id="error"))
        await editor.mount_all(widgets)
        if entry["kind"] == "enum":
            values = [m.value for m in entry["cls"]]
            if value in values:
                editor.query_one("#enum", OptionList).highlighted = values.index(value)
        self.update_exists()
        doc = entry["doc"] or "No description."
        self.query_one("#doc-text", Static).update(Text(doc))
        self.query_one("#doc", VerticalScroll).scroll_home(animate=False)

    def entry(self) -> dict:
        return self.section.entries[self.editor_key]

    def error(self, message: str) -> None:
        self.query_one("#error", Label).update(message)

    def update_exists(self) -> None:
        if not self.query("#exists"):
            return
        entry = self.entry()
        if not entry["value"]:
            text = Text("not set" + (" (will be asked for when needed)" if entry["required"] else ""), style="dim")
        elif path_exists(entry):
            text = Text("✓ exists", style="green")
        else:
            text = Text("✗ doesn't exist (saving is still allowed)", style="red")
        self.query_one("#exists", Label).update(text)

    @on(Input.Submitted, "#path")
    def path_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.set_value(self.editor_key, os.path.expanduser(event.value.strip()))
        self.update_exists()

    def action_browse(self) -> None:
        if not self.query("#path"):
            return
        entry = self.entry()
        start = os.path.dirname(entry["cls"](entry["value"]).resolve()) if entry["value"] else os.path.expanduser("~")
        folder = entry["kind"] == "folder"

        def chosen(path: Path | None) -> None:
            if path is not None:
                target = str(path)
                self.query_one("#path", PathInput).value = target
                self.set_value(self.editor_key, target)
                self.update_exists()

        self.push_screen(FilePicker(f"Choose {self.editor_key}", [""], "settings_browse_dir",
                                    start if os.path.isdir(start) else os.path.expanduser("~"), folders=folder), chosen)

    @on(Checkbox.Changed, "#bool")
    def bool_changed(self, event: Checkbox.Changed) -> None:
        self.set_value(self.editor_key, event.value)

    @on(OptionList.OptionSelected, "#enum")
    def enum_selected(self, event: OptionList.OptionSelected) -> None:
        self.set_value(self.editor_key, list(self.entry()["cls"])[event.option_index].value)

    @on(Input.Submitted, "#int")
    def int_submitted(self, event: Input.Submitted) -> None:
        try:
            self.set_value(self.editor_key, int(event.value))
            self.error("")
        except ValueError:
            self.error("Not a number.")

    @on(Input.Submitted, "#str")
    def str_submitted(self, event: Input.Submitted) -> None:
        self.set_value(self.editor_key, event.value)

    @on(TextArea.Changed, "#yaml")
    def yaml_changed(self, event: TextArea.Changed) -> None:
        try:
            value = yaml.safe_load(event.text_area.text)
        except yaml.YAMLError as e:
            self.error(f"Invalid YAML: {str(e).splitlines()[0]}")
            return
        self.error("")
        self.set_value(self.editor_key, value)

    async def action_reset(self) -> None:
        if self.editor_key and self.section and self.section.editable:
            self.set_value(self.editor_key, self.entry()["default"])
            await self.build_editor()

    # save / external editor / quit

    def action_save(self) -> None:
        if not self.dirty:
            self.notify("No changes to save")
            return
        current = settings.get_settings()
        missing = []
        for key, names in self.changed.items():
            section = self.sections[key]
            section.group.update({name: section.entries[name]["value"] for name in names})
            missing += [f"{key}.{name}" for name in names
                        if section.entries[name]["kind"] in ("file", "folder") and section.entries[name]["value"]
                        and not path_exists(section.entries[name])]
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

    async def action_quit(self) -> None:
        if self.dirty and not self.quit_armed:
            self.quit_armed = True
            self.notify("Unsaved changes. Press Ctrl+Q again to quit without saving, or Ctrl+S to save.",
                        severity="warning", timeout=6)
            self.set_timer(6, lambda: setattr(self, "quit_armed", False))
            return
        self.exit()


def run(*args: str) -> None:
    from aplite.tui import quiet_terminal
    with quiet_terminal():
        SettingsApp().run()
