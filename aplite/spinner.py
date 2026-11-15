"""A small spinner for waits: - + * + in shifting rainbow colors (characters every console font has)."""
from __future__ import annotations

import time

from rich.text import Text
from textual.color import Color
from textual.widget import Widget

FRAMES = "-+*+"
INTERVAL = 0.1


def frame(n: int) -> Text:
    """Frame `n`: the character, and a hue 30 degrees on from the last frame's."""
    return Text(FRAMES[n % len(FRAMES)], style=Color.from_hsl(n * 30 % 360 / 360, 0.9, 0.6).hex)


class Spinner(Widget):
    """The spinner and a space, to put before a message; animates only while shown (toggle `display`)."""
    DEFAULT_CSS = "Spinner { width: 2; height: 1; }"

    def on_show(self) -> None:
        self.auto_refresh = INTERVAL

    def on_hide(self) -> None:
        self.auto_refresh = None

    def render(self) -> Text:
        return frame(int(time.monotonic() / INTERVAL))
