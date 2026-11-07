"""
Shared skeleton of aplite's editors (Options Creator, settings editor, self-host settings): a side list (games /
sections), a list of rows, and a detail pane with an editor for the highlighted row plus its description. A Screen, so
it can be pushed over another app's screen (self-host settings, closable: Esc on the rows closes it); the full editors
run it in an EditorApp.

Subclasses compose these ids: #side (containing #side-filter and #side-list), #rows, #detail (containing #editor and
#doc > #doc-text), and implement row(key) and build_editor(). Only the highlighted row gets editor widgets, built once
the highlight rests, so long lists stay fast.
"""
from __future__ import annotations

import asyncio
import functools
import os
import typing

import yaml
from rich.text import Text
from textual import on
from textual.actions import SkipAction
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Input, Label, OptionList, SelectionList, Static, TextArea
from textual.widgets.option_list import Option as ListOption

from aplite import glyphs
from aplite.combo import FilterInput, first_enabled, highlight_first
from aplite.file_picker import FilePicker, PathInput
from aplite.nav import AppliteApp

if typing.TYPE_CHECKING:
    from pathlib import Path

EDITOR_DELAY = 0.06
"""Seconds the row highlight must rest before its editor is built, so holding an arrow key only scrolls."""
CONFIRM_SECONDS = 6
"""How long a "press again to confirm" warning stays armed."""
SIDE_WIDTH = 30
"""Columns of the side list; its entries are cut to fit (SIDE_TEXT_WIDTH, leaving room for border and padding)."""
SIDE_TEXT_WIDTH = SIDE_WIDTH - 4
MIN_ROW_WIDTH = 30
"""Rows are fitted to at least this many columns, even if #rows hasn't been laid out yet."""
MIN_VALUE_WIDTH = 16
"""Columns kept for a row's value when names are long (names get cut first)."""
EDITOR_SHARE = 0.6
"""Part of the detail column the editor may take (the rest shows the docs)."""
MAX_LIST_HEIGHT = 20
"""Most rows an editor list shows before scrolling."""
NOT_A_NUMBER = "Not a number."
PATH_PLACEHOLDER = "Path. Enter sets, Ctrl+Space completes, Ctrl+O browses"

CSS = f"""
#side {{ width: {SIDE_WIDTH}; }}
#side-list {{ height: 1fr; }}  /* OptionList defaults to max-height 100%, which ignores #side-filter above it */
#rows {{ width: 3fr; }}
#detail {{ width: 2fr; }}
#editor {{ height: auto; max-height: {round(EDITOR_SHARE * 100)}%; }}
#editor > OptionList, #editor > SelectionList {{ height: auto; max-height: {MAX_LIST_HEIGHT}; visibility: hidden; }}
#editor TextArea {{ height: 12; }}
#editor > Label {{ padding: 0 1; }}
#editor > .heading-end {{ padding: 0 1; }}
#doc {{ height: 1fr; border-top: solid $panel; padding: 0 1; }}
#doc:focus {{ border-top: solid $border; }}
#error {{ color: $error; }}
"""


def fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[:max(width - 1, 0)] + "…"


def describe(value: typing.Any) -> str:
    """Short text for a value in a row."""
    if isinstance(value, dict):
        return f"{{{len(value)} entries}}" if value else "{}"
    if isinstance(value, list):
        text = ", ".join(map(str, value))
        return f"[{text}]" if len(text) < 30 else f"[{len(value)} entries]"
    return str(value)


def to_yaml(value: typing.Any) -> str:
    """Text for a YAML editor ("" for empty values)."""
    if value in (None, ""):
        return ""
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True).replace("...\n", "")


def cycle(values: list, current: typing.Any, delta: int) -> typing.Any:
    """The next (delta > 0) or previous entry of `values` after `current`; from a value not in the list, the first or
    last one."""
    index = values.index(current) if current in values else (-1 if delta > 0 else 0)
    return values[(index + (1 if delta > 0 else -1)) % len(values)]


def side_pane(placeholder: str, *options: ListOption) -> Vertical:
    """#side: the filter and the list of games / sections (see MasterDetailScreen.side_entries)."""
    return Vertical(FilterInput(placeholder=placeholder, id="side-filter", target="#side-list"),
                    OptionList(*options, id="side-list"), id="side")


class TextScroll(VerticalScroll):
    """Scrolling text that Tab only stops at when there's something to scroll (focus must show; see its CSS)."""

    def allow_focus(self) -> bool:
        return self.max_scroll_y > 0


def detail_pane() -> Vertical:
    """#detail: the highlighted row's editor above its description."""
    return Vertical(Vertical(id="editor"), TextScroll(Static(id="doc-text"), id="doc"), id="detail")


def heading(name: str, details: str, end: bool = True) -> list[Label]:
    """The editor's first lines: the row's name and a dim line (type, default...). `end`: the last heading line."""
    return [Label(Text(name, style="bold")),
            Label(Text(details, style="dim"), classes="heading-end" if end else "")]


def yaml_editor(value: typing.Any, hint: str) -> list:
    return [Label(Text(hint, style="dim")), TextArea(to_yaml(value), id="yaml")]


def editor_event(handler: typing.Callable) -> typing.Callable:
    """For handlers of editor widget events: drops events from widgets a rebuild already removed, which would otherwise
    edit whatever row the editor shows now (or query widgets that are gone), and events during a build (Select.Changed
    on mount, before its siblings are mounted: it only repeats the value the editor was built with)."""
    @functools.wraps(handler)
    def wrapper(self, event=None) -> None:  # event None: called directly
        if event is None or event.control is None or event.control.is_attached and not self._editor_lock.locked():
            handler(self, event)
    return wrapper


class Rows(OptionList):
    """The rows, grid-like (research/keyboard.md): Left/Right step the value in place, Enter/Space edit it."""
    BINDINGS = [Binding("left", "screen.step(-1)", show=False), Binding("right", "screen.step(1)", show=False),
                Binding("shift+left", "screen.step(-10)", show=False),
                Binding("shift+right", "screen.step(10)", show=False),
                Binding("space", "screen.edit", show=False)]

    def on_resize(self) -> None:
        """Rows are fitted to this width by hand: refit whenever it changes (first layout included; rows filled
        before it, e.g. while #main was still hidden, were fitted to MIN_ROW_WIDTH)."""
        screen = self.screen
        if isinstance(screen, MasterDetailScreen):
            screen.call_after_refresh(screen.refresh_rows)


class EditorApp(AppliteApp):
    """Runs one editor screen as a whole app (pushed, not the default screen: a default screen's CSS isn't loaded)."""

    def __init__(self, screen: MasterDetailScreen, title: str) -> None:
        super().__init__()
        self.editor = screen
        self.title = title

    def on_mount(self) -> None:
        self.push_screen(self.editor)


class MasterDetailScreen(Screen):
    BINDINGS = [Binding("escape", "back_to_rows", show=False)]
    CSS = CSS
    NARROW = 140
    """Below this many columns, opening a game/section hides the side list (Ctrl+G brings it back)."""
    BROWSE_PREF = "settings_browse_dir"
    """Pref remembering the folder Ctrl+O (browse for a path) was last in."""
    STRIP_TEXT = False
    """Strip spaces from text values (#str)."""
    MISSING_NOTE = ""
    """Added to "doesn't exist" for a path row."""
    dirty: bool = False
    """Unsaved changes."""

    def __init__(self, closable: bool = False) -> None:
        super().__init__()
        self.closable = closable  # Esc outside the editor closes the screen (pushed over another app's screen)
        self.current: str | None = None  # highlighted row
        self.editor_key: str | None = None  # row the editor widgets belong to; their events edit this one
        self.name_width = 10  # longest row name in the current list
        self._editor_timer = None
        self._editor_lock = asyncio.Lock()  # one build at a time: a timer firing mid-build mounted duplicate ids
        self._armed: set[str] = set()
        self._list_heights: dict[tuple, int] = {}  # editor_shape() -> fitted list max-height, until a resize

    # rows

    def row(self, key: str) -> Text:
        raise NotImplementedError

    def row_keys(self) -> typing.Iterable[str]:
        """Keys of the rows currently listed."""
        raise NotImplementedError

    async def build_editor(self) -> None:
        raise NotImplementedError

    def set_value(self, key: str, value: typing.Any) -> None:
        raise NotImplementedError

    def stepped(self, key: str, delta: int) -> typing.Any:
        """The value Left/Right (delta -1/1, Shift: -10/10) gives the row, or None if it can't be stepped."""
        return None

    def update_editor(self, key: str) -> bool:
        """Show a stepped value in the editor already built for `key`, without rebuilding it; False: rebuild."""
        return False

    def default_of(self, key: str) -> typing.Any:
        """The value Ctrl+R resets a row to."""
        raise NotImplementedError

    def side_entries(self, search: str) -> list[ListOption]:
        """The side list's entries matching the filter text (casefolded); their ids go to open_side."""
        raise NotImplementedError

    def open_side(self, key: str) -> None:
        """Show a side list entry (game, section) and move to its rows."""
        raise NotImplementedError

    def fitted_row(self, name: str, value: str, changed: bool, note: Text | None = None) -> Text:
        """One line fitted to #rows by hand (OptionList wraps long prompts): name column, then the value."""
        width = max(MIN_ROW_WIDTH, self.query_one("#rows", OptionList).scrollable_content_region.width - 1)
        name_width = min(self.name_width, width - MIN_VALUE_WIDTH)
        text = Text("  " + fit(name, name_width).ljust(name_width + 2))
        note_width = len(note) if note else 0
        text.append(fit(value, width - name_width - 4 - note_width), style="bold" if changed else "dim")
        if note:
            text.append(note)
        return text

    def refresh_row(self, key: str) -> None:
        self.query_one("#rows", OptionList).replace_option_prompt(key, self.row(key))

    def refresh_rows(self) -> None:
        for key in self.row_keys():
            self.refresh_row(key)

    def on_resize(self) -> None:
        self._list_heights.clear()
        self.call_after_refresh(self.refresh_rows)
        self.call_after_refresh(self.fit_editor)

    @on(OptionList.OptionHighlighted, "#rows")
    def _row_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id and event.option.id != self.current:
            self.current = event.option.id
            self._schedule_editor()

    def _schedule_editor(self) -> None:
        if self._editor_timer:
            self._editor_timer.stop()
        self._editor_timer = self.set_timer(EDITOR_DELAY, self._build_editor_now)

    async def _build_editor_now(self) -> None:
        self._editor_timer = None
        async with self._editor_lock:
            if not self.query("#editor"):  # the timer fired while the app was closing
                return
            await self.query_one("#editor").remove_children()
            self.editor_key = self.current
            if self.editor_key is not None:
                await self.build_editor()
                self.call_after_refresh(self.fit_editor)
            self.refresh_bindings()  # Browse shows in the footer only for path editors (check_action)

    def action_step(self, delta: int) -> None:
        if self.current is None:
            return
        value = self.stepped(self.current, delta)
        if value is not None:
            self.set_value(self.current, value)
            if self.editor_key == self.current and self.query("#error"):
                self.error("")  # a stepped value is valid; an old message (e.g. "Not a number.") no longer applies
            # a rebuild scrolls a long list back to the top for a frame: update in place when the editor is built
            if self._editor_timer or self.editor_key != self.current or not self.update_editor(self.current):
                self._schedule_editor()

    @on(OptionList.OptionSelected, "#rows")
    async def action_edit(self) -> None:
        """Enter/Space on a row: focus the editor's value widget (the first focusable one; Random only if it's the
        only one left enabled)."""
        if self.current is None:
            return
        if self.editor_key != self.current or self._editor_timer:
            if self._editor_timer:
                self._editor_timer.stop()
            await self._build_editor_now()
        focusable = [widget for widget in self.query_one("#editor").query("*") if widget.focusable]
        preferred = [widget for widget in focusable if widget.id != "random"
                     and not any(ancestor.id == "random-opts" for ancestor in widget.ancestors)]
        if preferred or focusable:
            (preferred or focusable)[0].focus()

    def action_back_to_rows(self) -> None:
        """Esc in the editor; on the rows it goes to the side list (shown if hidden); elsewhere it closes a closable
        screen."""
        focused = self.focused
        if focused is not None and focused.id == "rows" and self.query("#side"):
            side = self.query_one("#side")
            if not side.display:
                side.display = True
                self.call_after_refresh(self.refresh_rows)
            self.query_one("#side-filter", Input).focus()
            return
        if focused is None or not any(ancestor.id == "editor" for ancestor in focused.ancestors):
            if not self.closable:
                raise SkipAction()
            if not self.dirty or self.confirm_twice(
                    "close", "Unsaved changes. Press Esc again to close without saving, or Ctrl+S to save."):
                self.dismiss(None)
            return
        self.back_to_rows()

    def back_to_rows(self) -> None:
        """After a single value is set (choice, number, text...) or Esc: back to the row, like a closed dropdown."""
        self.query_one("#rows", OptionList).focus()

    def show_rows(self, rows: list) -> None:
        """Replace the rows (and highlight the first selectable one); the editor follows the highlight."""
        self.current = None
        options = self.query_one("#rows", OptionList)
        options.clear_options()
        options.add_options(rows)
        if first_enabled(options) is not None:
            highlight_first(options)

    async def action_reset(self) -> None:
        if self.editor_key is not None:
            self.set_value(self.editor_key, self.default_of(self.editor_key))
            await self._build_editor_now()

    # side list

    def fill_side(self) -> None:
        search = self.query_one("#side-filter", Input).value.strip().casefold()
        side = self.query_one("#side-list", OptionList)
        side.clear_options()
        side.add_options(self.side_entries(search))
        highlight_first(side)

    @on(Input.Changed, "#side-filter")
    def _side_filter_changed(self) -> None:
        self.fill_side()

    @on(Input.Submitted, "#side-filter")
    def _side_filter_submitted(self) -> None:
        option = self.query_one("#side-list", OptionList).highlighted_option
        if option:
            self.open_side(option.id)

    @on(OptionList.OptionSelected, "#side-list")
    def _side_selected(self, event: OptionList.OptionSelected) -> None:
        self.open_side(event.option.id)

    def collapse_side_if_narrow(self) -> None:
        side = self.query_one("#side")
        if self.size.width < self.NARROW and side.display:
            side.display = False
            self.call_after_refresh(self.refresh_rows)  # the rows just got wider

    def action_toggle_side(self) -> None:
        side = self.query_one("#side")
        side.display = not side.display
        (self.query_one("#side-filter", Input) if side.display else self.query_one("#rows", OptionList)).focus()
        self.call_after_refresh(self.refresh_rows)

    # detail pane helpers

    @staticmethod
    def editor_shape(widgets: typing.Iterable) -> tuple:
        """Editors with the same shape fit their lists to the same height."""
        return tuple((type(widget).__name__, widget.id, widget.display) for widget in widgets)

    async def mount_editor(self, widgets: list) -> None:
        """Mount the editor widgets, lists already at the height fitted for this shape last time, so they are laid
        out once (fitting after the first layout made the widgets below a list jump)."""
        height = self._list_heights.get(self.editor_shape(widgets))
        if height is not None:
            for widget in widgets:
                if isinstance(widget, (OptionList, SelectionList)):
                    widget.styles.max_height = height
                    widget.styles.visibility = "visible"
        await self.query_one("#editor").mount_all(widgets)

    def fit_editor(self) -> None:
        """Shrink the editor's lists to the room the other editor widgets leave: a list taller than that got cut off
        by #editor's max-height without scrolling."""
        if not self.query("#editor"):
            return
        editor = self.query_one("#editor")
        lists = [child for child in editor.children if isinstance(child, (OptionList, SelectionList))]
        if not lists:
            return
        room = int(self.query_one("#detail").size.height * EDITOR_SHARE)
        room -= sum(child.outer_size.height for child in editor.children if child not in lists)
        height = max(3, min(MAX_LIST_HEIGHT, room // len(lists)))
        self._list_heights[self.editor_shape(editor.children)] = height
        for widget in lists:
            widget.styles.max_height = height
            widget.styles.visibility = "visible"  # hidden until fitted the first time: unfitted, it showed cut off

    def show_doc(self, text: str) -> None:
        self.query_one("#doc-text", Static).update(Text(text))
        self.query_one("#doc", VerticalScroll).scroll_home(animate=False)

    def error(self, message: str) -> None:
        # query, not query_one: an editor widget's event (Select.Changed on mount) can arrive before #error is mounted
        for label in self.query("#error").results(Label):
            label.update(message)

    def parse_yaml(self, text: str) -> tuple[bool, typing.Any]:
        """(ok, value) for a YAML editor's text; shows the parse error in #error."""
        try:
            value = yaml.safe_load(text)
        except yaml.YAMLError as e:
            self.error(f"Invalid YAML: {str(e).splitlines()[0]}")
            return False, None
        self.error("")
        return True, value

    def confirm_twice(self, action: str, message: str) -> bool:
        """For risky actions: the first call warns and returns False; calling again within CONFIRM_SECONDS → True."""
        if action in self._armed:
            self._armed.discard(action)
            return True
        self._armed.add(action)
        self.notify(message, severity="warning", timeout=CONFIRM_SECONDS)
        self.set_timer(CONFIRM_SECONDS, lambda: self._armed.discard(action))
        return False

    # simple editors (#int, #str) and paths (#path with an #exists line, Ctrl+O browses)

    @on(Input.Submitted, "#int")
    @editor_event
    def _int_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        try:
            number = int(event.value)
        except ValueError:
            self.error(NOT_A_NUMBER)
            return
        self.set_value(self.editor_key, number)
        self.error("")
        self.back_to_rows()

    @on(Input.Submitted, "#str")
    @editor_event
    def _str_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.set_value(self.editor_key, event.value.strip() if self.STRIP_TEXT else event.value)
        self.back_to_rows()

    @staticmethod
    def path_editor(value: str) -> list:
        return [PathInput(value, id="path", placeholder=PATH_PLACEHOLDER), Label("", id="exists")]

    def resolved_path(self, key: str) -> str:
        """A path row's value as an absolute path ("" when not set)."""
        raise NotImplementedError

    def path_note(self, key: str) -> str:
        """Added to "not set"."""
        return ""

    def picks_folder(self, key: str) -> bool:
        return False

    def update_exists(self) -> None:
        if not self.query("#exists"):
            return
        path = self.resolved_path(self.editor_key)
        if not path:
            text = Text("not set" + self.path_note(self.editor_key), style="dim")
        elif os.path.exists(path):
            text = Text(f"{glyphs.symbol('exists')} exists", style="green")
        else:
            text = Text("x doesn't exist" + self.MISSING_NOTE, style="red")
        self.query_one("#exists", Label).update(text)

    @on(Input.Submitted, "#path")
    @editor_event
    def _path_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.set_value(self.editor_key, os.path.expanduser(event.value.strip()))
        self.update_exists()
        self.back_to_rows()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        return bool(self.query("#path")) if action == "browse" else True

    def action_browse(self) -> None:
        if not self.query("#path"):
            return
        key = self.editor_key
        path = self.resolved_path(key)
        start = os.path.dirname(path) if path else None  # the path's folder; else the last one browsed, else home

        def chosen(picked: Path | None) -> None:
            if picked is not None:
                self.query_one("#path", PathInput).value = str(picked)
                self.set_value(key, str(picked))
                self.update_exists()

        self.app.push_screen(FilePicker(f"Choose {key}", [], self.BROWSE_PREF, os.path.expanduser("~"),
                                        folders=self.picks_folder(key), show_all=True, start=start), chosen)

    def can_leave(self, step: str) -> bool:
        """Asked by AppliteApp on ctrl+q / ctrl+l."""
        key, verb = ("Ctrl+Q", "quit") if step == "quit" else ("Ctrl+L", "go to the launcher")
        return not self.dirty or self.confirm_twice(
            step, f"Unsaved changes. Press {key} again to {verb} without saving, or Ctrl+S to save.")
