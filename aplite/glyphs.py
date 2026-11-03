"""Keeps Textual to glyphs every console font has (code page 437: box lines, full and half blocks, ♥►▼↑↓ and co.), so
nothing shows up as a missing-glyph box in conhost or with fonts like Consolas. Textual's fancier borders and its smooth
scrollbars use eighth blocks (▁▊▔...) and rounded or heavy lines that those fonts lack, and some widgets use emoji.
Applied on import of aplite.nav, before any app draws."""
from __future__ import annotations

import os

from textual import _border, keys
from textual.renderables.bar import Bar
from textual.scrollbar import ScrollBarRender
from textual.widgets import DirectoryTree

BORDER = os.environ.get("APLITE_BORDER", "solid")
"""Safe style drawn in place of every unsafe one (APLITE_BORDER: preview only)."""

SAFE_BORDERS = {"", "ascii", "none", "hidden", "blank", "solid", "double", "thick", "block"}
"""Border styles whose glyphs are all in code page 437."""


def apply() -> None:
    for name in list(_border.BORDER_CHARS):
        if name not in SAFE_BORDERS:
            _border.BORDER_CHARS[name] = _border.BORDER_CHARS[BORDER]
            _border.BORDER_LOCATIONS[name] = _border.BORDER_LOCATIONS[BORDER]
    _border.get_box.cache_clear()
    # half a cell of smoothness at the thumb's ends instead of an eighth
    ScrollBarRender.VERTICAL_BARS = ["▄", " "]
    ScrollBarRender.HORIZONTAL_BARS = ["▌", " "]
    # tab underline (TabbedContent)
    Bar.BAR = Bar.HALF_BAR_LEFT = Bar.HALF_BAR_RIGHT = "─"
    # emoji
    DirectoryTree.ICON_NODE, DirectoryTree.ICON_NODE_EXPANDED, DirectoryTree.ICON_FILE = "► ", "▼ ", "  "
    # footer key names
    keys.KEY_DISPLAY_ALIASES.update(backspace="bksp", enter="enter")


apply()
