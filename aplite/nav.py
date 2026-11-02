"""Keys every aplite app shares: ctrl+q quits aplite, ctrl+l goes (back) to the launcher, ctrl+p is Textual's command
palette, labelled Options. The app just ends; __main__ reads `next_step` to decide what runs after it."""
from __future__ import annotations

import typing

from textual.app import App
from textual.binding import Binding

next_step: typing.Literal["quit", "launcher"] | None = None
"""What the user asked for when the last app ended (None: it ended on its own)."""


class AppliteApp(App):
    BINDINGS = [Binding("ctrl+q", "leave('quit')", "Quit"), Binding("ctrl+l", "leave('launcher')", "Launcher"),
                Binding("ctrl+p", "command_palette", "Options", show=False)]  # the footer shows it at the right

    def can_leave(self, step: str) -> bool:
        """Override to guard unsaved work; called on every ctrl+q / ctrl+l."""
        return True

    def action_leave(self, step: typing.Literal["quit", "launcher"]) -> None:
        global next_step
        if self.can_leave(step):
            next_step = step
            self.exit()

    async def action_quit(self) -> None:  # the command palette's "Quit"
        self.action_leave("quit")
