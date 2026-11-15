"""A small spinner for waits: - + o * o + in shifting rainbow colors (characters every console font has). Now and then
(ALT_CHANCE) a spinner cycles through a pride flag's colors instead."""
from __future__ import annotations

import random
import time

from rich.text import Text
from textual.color import Color
from textual.widget import Widget

FRAMES = "-+o*o+"
INTERVAL = 0.1
ALT_CHANCE = 1  # TEMP: testing, was 0.05
LIGHT, DARK = "#D0D0D0", "#5C5C5C"  # the flags' white and black, so no frame vanishes on a light or dark theme
ALT_PALETTES = {
    "trans": ("#5BCEFA", "#F5A9B8", LIGHT),
    "lesbian": ("#D52D00", "#FF9A56", LIGHT, "#D362A4", "#A30262"),
    "progress": ("#E40303", "#FF8C00", "#FFED00", "#008026", "#004DFF", "#750787",
                 DARK, "#784F17", "#5BCEFA", "#F5A9B8", LIGHT),
    "ace": (DARK, "#A3A3A3", LIGHT, "#800080"),
}


def pick_palette() -> tuple[str, ...] | None:
    """A palette for a new spinner: None (the rainbow) most of the time, else a random flag's colors."""
    return random.choice(list(ALT_PALETTES.values())) if random.random() < ALT_CHANCE else None


def frame(n: int, palette: tuple[str, ...] | None = None) -> Text:
    """Frame `n`: the character, in the palette's next color, or a hue 30 degrees on from the last frame's."""
    color = palette[n % len(palette)] if palette else Color.from_hsl(n * 30 % 360 / 360, 0.9, 0.6).hex
    return Text(FRAMES[n % len(FRAMES)], style=color)


class Spinner(Widget):
    """The spinner and a space, to put before a message; animates only while shown (toggle `display`)."""
    DEFAULT_CSS = "Spinner { width: 2; height: 1; }"
    palette: tuple[str, ...] | None = None

    def on_show(self) -> None:
        self.palette = pick_palette()
        self.auto_refresh = INTERVAL

    def on_hide(self) -> None:
        self.auto_refresh = None

    def render(self) -> Text:
        return frame(int(time.monotonic() / INTERVAL), self.palette)
