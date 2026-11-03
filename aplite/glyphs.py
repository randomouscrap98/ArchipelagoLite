"""Keeps Textual to glyphs that consoles have, so nothing shows up as a missing-glyph box. Textual's own borders and
smooth scrollbars use eighth blocks (▁▊▔...) and rounded or heavy lines that conhost's fonts (Consolas) and Linux tty
fonts lack, and some widgets use emoji. Two modes, both with thin outlines:
  fancy   focused boxes: thin lines on the box's background; others: half blocks with quadrant corners in the box's
          colour (the box shrinks a little). Half-block scrollbar ends. Needs block elements: Windows Terminal, most
          terminal emulators.
  simple  thin lines on the parent's background: border colour while focused, else the box's colour. Plain scrollbars.
          Only box-drawing lines: works in conhost and the Linux tty too.
Mode: APLITE_GLYPHS env, else the "glyphs" pref (Options menu), else simple on conhost and the Linux tty. Applied on
import of aplite.nav, before any app draws; set_mode() switches while running."""
from __future__ import annotations

import functools
import os
import sys
import typing

from rich.segment import Segment
from textual import _border, _styles_cache, keys
from textual.renderables.bar import Bar
from textual.scrollbar import ScrollBarRender
from textual.style import Style
from textual.widgets import DirectoryTree

from aplite import prefs

if typing.TYPE_CHECKING:
    from textual.app import App

Mode = typing.Literal["fancy", "simple"]

SAFE_BORDERS = {"", "ascii", "none", "hidden", "blank", "solid", "double", "thick", "block"}
"""Border styles whose glyphs are all in code page 437 (left alone). Every other one is drawn the mode's way."""
OUTER = ((1, 1, 1), (1, 0, 1), (1, 1, 1))
"""Border cells on the parent's background (Textual's location 1)."""

FOCUSED = {"fancy": (_border.BORDER_CHARS["solid"], _border.BORDER_LOCATIONS["solid"]),
           "simple": (_border.BORDER_CHARS["solid"], OUTER)}
"""Focused boxes: Textual border glyphs and locations, drawn in the border colour."""
BLURRED = {"fancy": (("▗", "▄", "▖"), ("▐", " ", "▌"), ("▝", "▀", "▘")),
           "simple": _border.BORDER_CHARS["solid"]}
"""Other boxes: glyphs drawn in the box's colour on the parent's background."""
SCROLLBAR_ENDS = {"fancy": (["▄", " "], ["▌", " "]), "simple": ([" "], [" "])}
"""ScrollBarRender's vertical and horizontal bars: half a cell of smoothness, or none."""

UNSAFE = {name for name in _border.BORDER_CHARS if name not in SAFE_BORDERS}
mode: Mode = "fancy"
_focused = False
"""Whether the widget being rendered has focus (rendering is synchronous)."""


def default_mode() -> Mode:
    """Simple where block elements are missing: conhost (Windows outside Windows Terminal) and the Linux tty."""
    if sys.platform == "win32" and not os.environ.get("WT_SESSION") or os.environ.get("TERM") == "linux":
        return "simple"
    return "fancy"


def chosen_mode() -> Mode:
    for choice in (os.environ.get("APLITE_GLYPHS"), prefs.get("glyphs")):
        if choice in ("fancy", "simple"):
            return choice
    return default_mode()


def apply(new_mode: Mode) -> None:
    global mode
    mode = new_mode
    chars, locations = FOCUSED[mode]
    for name in UNSAFE:
        _border.BORDER_CHARS[name] = chars
        _border.BORDER_LOCATIONS[name] = locations
    _border.get_box.cache_clear()
    _blurred_box.cache_clear()
    ScrollBarRender.VERTICAL_BARS, ScrollBarRender.HORIZONTAL_BARS = SCROLLBAR_ENDS[mode]


def set_mode(app: App, new_mode: Mode) -> None:
    """Switch modes in a running app (saved as the "glyphs" pref) and redraw everything."""
    prefs.put("glyphs", new_mode)
    apply(new_mode)
    for screen in app.screen_stack:
        for widget in [screen, *screen.walk_children()]:
            widget._styles_cache.clear()
            widget.refresh()


@functools.lru_cache(maxsize=1024)
def _blurred_box(inner_style: Style, outer_style: Style):
    glyph = Style(background=outer_style.background, foreground=inner_style.background).rich_style
    inside = inner_style.rich_style
    return tuple(tuple(Segment(c, inside if c == " " else glyph) for c in row) for row in BLURRED[mode])


def _install() -> None:
    get_box, render_widget = _styles_cache.get_box, _styles_cache.StylesCache.render_widget

    def get_box_by_focus(name, inner_style, outer_style, style):
        if name in UNSAFE and not _focused:
            return _blurred_box(inner_style, outer_style)
        return get_box(name, inner_style, outer_style, style)

    def render_widget_noting_focus(self, widget, crop):
        global _focused
        _focused = widget.has_focus
        return render_widget(self, widget, crop)

    _styles_cache.get_box = get_box_by_focus
    _styles_cache.StylesCache.render_widget = render_widget_noting_focus
    # tab underline (TabbedContent)
    Bar.BAR = Bar.HALF_BAR_LEFT = Bar.HALF_BAR_RIGHT = "─"
    # emoji
    DirectoryTree.ICON_NODE, DirectoryTree.ICON_NODE_EXPANDED, DirectoryTree.ICON_FILE = "► ", "▼ ", "  "
    # footer key names
    keys.KEY_DISPLAY_ALIASES.update(backspace="bksp", enter="enter")


_install()
apply(chosen_mode())
