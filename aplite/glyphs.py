"""Keeps Textual to glyphs every console font has (code page 437: box lines, full and half blocks, ♥►▼↑↓ and co.), so
nothing shows up as a missing-glyph box in conhost or with fonts like Consolas. Textual's fancier borders and its smooth
scrollbars use eighth blocks (▁▊▔...) and rounded or heavy lines that those fonts lack, and some widgets use emoji.
Applied on import of aplite.nav, before any app draws."""
from __future__ import annotations

import functools
import os

from rich.segment import Segment
from textual import _border, _styles_cache, keys
from textual.renderables.bar import Bar
from textual.scrollbar import ScrollBarRender
from textual.style import Style
from textual.widgets import DirectoryTree

BORDER = os.environ.get("APLITE_BORDER", "outline")
"""Safe style drawn in place of every unsafe one (APLITE_BORDER: preview only), or one of FOCUS_STYLES."""

SAFE_BORDERS = {"", "ascii", "none", "hidden", "blank", "solid", "double", "thick", "block"}
"""Border styles whose glyphs are all in code page 437."""

OUTER = ((1, 1, 1), (1, 0, 1), (1, 1, 1))
"""Border cells on the parent's background (Textual's location 1), so touching boxes stay apart."""
PREVIEWS = {
    # thin lines, like solid, but drawn on the parent's background
    "line": (_border.BORDER_CHARS["solid"], OUTER),
    # half blocks hugging the content: a half-cell frame in the border colour, a half-cell gap outside it
    "half": ((("▄", "▄", "▄"), ("▐", " ", "▌"), ("▀", "▀", "▀")), OUTER),
    "halfcut": (((" ", "▄", " "), ("▐", " ", "▌"), (" ", "▀", " ")), OUTER),
}
"""Extra choices for APLITE_BORDER while picking one."""
FOCUS_STYLES = {
    # solid on the box's background while focused, else half blocks in the box's colour (the box shrinks a little)
    "hybrid": ("solid", (("▄", "▄", "▄"), ("▐", " ", "▌"), ("▀", "▀", "▀"))),
    # thin lines on the parent's background: border colour while focused, else the box's colour
    "outline": ("line", _border.BORDER_CHARS["solid"]),
}
"""name: (style while focused, glyphs drawn in the box's colour on the parent's background while not)."""

_focused = False
"""Whether the widget being rendered has focus (rendering is synchronous)."""


def apply() -> None:
    style = FOCUS_STYLES[BORDER][0] if BORDER in FOCUS_STYLES else BORDER
    chars, locations = PREVIEWS.get(style) or (_border.BORDER_CHARS[style], _border.BORDER_LOCATIONS[style])
    unsafe = {name for name in _border.BORDER_CHARS if name not in SAFE_BORDERS}
    for name in unsafe:
        _border.BORDER_CHARS[name] = chars
        _border.BORDER_LOCATIONS[name] = locations
    _border.get_box.cache_clear()
    if BORDER in FOCUS_STYLES:
        _by_focus(unsafe, FOCUS_STYLES[BORDER][1])
    # half a cell of smoothness at the thumb's ends instead of an eighth
    ScrollBarRender.VERTICAL_BARS = ["▄", " "]
    ScrollBarRender.HORIZONTAL_BARS = ["▌", " "]
    # tab underline (TabbedContent)
    Bar.BAR = Bar.HALF_BAR_LEFT = Bar.HALF_BAR_RIGHT = "─"
    # emoji
    DirectoryTree.ICON_NODE, DirectoryTree.ICON_NODE_EXPANDED, DirectoryTree.ICON_FILE = "► ", "▼ ", "  "
    # footer key names
    keys.KEY_DISPLAY_ALIASES.update(backspace="bksp", enter="enter")


def _by_focus(unsafe: set[str], blurred_glyphs) -> None:
    get_box, render_widget = _styles_cache.get_box, _styles_cache.StylesCache.render_widget

    @functools.lru_cache(maxsize=1024)
    def blurred_box(inner_style: Style, outer_style: Style):
        glyph = Style(background=outer_style.background, foreground=inner_style.background).rich_style
        inside = inner_style.rich_style
        return tuple(tuple(Segment(c, inside if c == " " else glyph) for c in row) for row in blurred_glyphs)

    def focus_box(name, inner_style, outer_style, style):
        if name in unsafe and not _focused:
            return blurred_box(inner_style, outer_style)
        return get_box(name, inner_style, outer_style, style)

    def render_widget_noting_focus(self, widget, crop):
        global _focused
        _focused = widget.has_focus
        return render_widget(self, widget, crop)

    _styles_cache.get_box = focus_box
    _styles_cache.StylesCache.render_widget = render_widget_noting_focus


apply()
