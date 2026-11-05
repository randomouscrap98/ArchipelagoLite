"""Combobox keys (WAI-ARIA APG pattern, see research/keyboard.md): a filter Input that keeps focus while the list it
filters is moved with Up/Down/PgUp/PgDn. Enter submits as usual; handlers take the list's highlighted entry."""
from __future__ import annotations

from textual.actions import SkipAction
from textual.binding import Binding
from textual.widgets import Input, OptionList


class FilterInput(Input):
    BINDINGS = [Binding("up", "move('cursor_up')", show=False), Binding("down", "move('cursor_down')", show=False),
                Binding("pageup", "move('page_up')", show=False), Binding("pagedown", "move('page_down')", show=False),
                Binding("escape", "clear", show=False)]

    def __init__(self, *args, target: str, **kwargs) -> None:
        """`target`: selector of the OptionList (or SelectionList) this filters; it stops being a Tab stop."""
        super().__init__(*args, **kwargs)
        self.target = target

    def on_mount(self) -> None:
        self.list.can_focus = False

    @property
    def list(self) -> OptionList:
        return self.screen.query_one(self.target, OptionList)

    def action_move(self, action: str) -> None:
        getattr(self.list, f"action_{action}")()

    def action_clear(self) -> None:
        """Esc clears a filter; an empty one lets Esc through (back, cancel...)."""
        if not self.value:
            raise SkipAction()
        self.value = ""


def first_enabled(options: OptionList) -> int | None:
    """Index of the list's first selectable entry."""
    return next((i for i, option in enumerate(options.options) if not option.disabled), None)


def highlight_first(options: OptionList) -> None:
    """After refilling a filtered list: highlight its first selectable entry."""
    options.highlighted = first_enabled(options)
