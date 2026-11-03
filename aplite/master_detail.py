"""
Shared skeleton of aplite's editors (Options Creator, settings editor): a side list (games / sections), a list of
rows, and a detail pane with an editor for the highlighted row plus its description.

Subclasses compose these ids: #side (containing #side-filter and #side-list), #rows, #detail (containing #editor and
#doc > #doc-text), and implement row(key) and build_editor(). Only the highlighted row gets editor widgets, built once
the highlight rests, so long lists stay fast.
"""
from __future__ import annotations

import asyncio
import functools
import typing

import yaml
from rich.text import Text
from textual import on
from textual.actions import SkipAction
from textual.binding import Binding
from textual.widgets import Input, Label, OptionList, SelectionList

from aplite.nav import AppliteApp

NARROW = 140
"""Below this many columns, opening a game/section hides the side list (Ctrl+G brings it back)."""
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

CSS = f"""
#side {{ width: {SIDE_WIDTH}; }}
#rows {{ width: 3fr; }}
#detail {{ width: 2fr; }}
#editor {{ height: auto; max-height: {round(EDITOR_SHARE * 100)}%; }}
#editor OptionList, #editor SelectionList {{ height: auto; max-height: {MAX_LIST_HEIGHT}; visibility: hidden; }}
#editor TextArea {{ height: 12; }}
#editor > Label {{ padding: 0 1; }}
#editor > .heading-end {{ padding: 0 1 1 1; }}
#doc {{ height: 1fr; border-top: solid $panel; padding: 0 1; }}
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


def editor_event(handler: typing.Callable) -> typing.Callable:
    """For handlers of editor widget events: drops events from widgets a rebuild already removed, which would otherwise
    edit whatever row the editor shows now (or query widgets that are gone)."""
    @functools.wraps(handler)
    def wrapper(self, event=None) -> None:  # event None: called directly
        if event is None or event.control is None or event.control.is_attached:
            handler(self, event)
    return wrapper


class Rows(OptionList):
    """The rows, grid-like (research/keyboard.md): Left/Right step the value in place, Enter/Space edit it."""
    BINDINGS = [Binding("left", "app.step(-1)", show=False), Binding("right", "app.step(1)", show=False),
                Binding("shift+left", "app.step(-10)", show=False), Binding("shift+right", "app.step(10)", show=False),
                Binding("space", "app.edit", show=False)]


class MasterDetailApp(AppliteApp):
    BINDINGS = [Binding("escape", "back_to_rows", show=False)]

    def __init__(self) -> None:
        super().__init__()
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
        """Esc in the editor."""
        focused = self.focused
        if focused is None or not any(ancestor.id == "editor" for ancestor in focused.ancestors):
            raise SkipAction()
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
        first = next((i for i, option in enumerate(options.options) if not option.disabled), None)
        if first is not None:
            options.highlighted = first

    # side list

    def collapse_side_if_narrow(self) -> None:
        side = self.query_one("#side")
        if self.size.width < NARROW and side.display:
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

    def error(self, message: str) -> None:
        self.query_one("#error", Label).update(message)

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

    dirty: bool  # unsaved changes

    def can_leave(self, step: str) -> bool:
        key, verb = ("Ctrl+Q", "quit") if step == "quit" else ("Ctrl+L", "go to the launcher")
        return not self.dirty or self.confirm_twice(
            step, f"Unsaved changes. Press {key} again to {verb} without saving, or Ctrl+S to save.")
