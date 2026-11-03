"""Keys every aplite app shares: ctrl+q quits aplite, ctrl+l goes (back) to the launcher, ctrl+p is Textual's command
palette, labelled Options. The app just ends; __main__ reads `next_step` to decide what runs after it."""
from __future__ import annotations

import functools
import typing

from textual.app import App
from textual.binding import Binding

next_step: typing.Literal["quit", "launcher"] | None = None
"""What the user asked for when the last app ended (None: it ended on its own)."""

QUIT_STATUS = 86
"""Exit status of a child process (file picker, command-line script) whose user pressed ctrl+q: quit aplite."""


class AppliteApp(App):
    BINDINGS = [Binding("ctrl+q", "leave('quit')", "Quit"), Binding("ctrl+l", "leave('launcher')", "Launcher"),
                Binding("ctrl+p", "command_palette", "Options", show=False)]  # the footer shows it at the right

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """While another screen is on top (file picker, save dialog), the app's own keys would act on the hidden
        screen below: hide them. Shared keys and Textual's own (Tab focus etc.) stay."""
        return len(self.screen_stack) == 1 or action not in _own_actions(type(self))

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


@functools.cache
def _own_actions(cls: type) -> frozenset[str]:
    """Actions bound by an AppliteApp subclass's own BINDINGS (not AppliteApp's shared ones, not Textual's)."""
    actions = set()
    for klass in cls.__mro__:
        if klass is AppliteApp:
            break
        for binding in klass.__dict__.get("BINDINGS", ()):
            action = binding.action if isinstance(binding, Binding) else binding[1]
            actions.add(action.split("(")[0])
    return frozenset(actions)
