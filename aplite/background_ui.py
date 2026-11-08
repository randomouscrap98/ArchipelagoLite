"""The "Background spares" option (Options palette): how many spare aplite processes background.py keeps loaded."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

import Utils
from aplite import background, prefs


def count() -> int:
    return int(prefs.get(background.PREF) or 0)


def size_label(size: float) -> str:
    return f"{size / 2 ** 30:.1f} GB" if size >= 2 ** 30 else f"{size / 2 ** 20:.0f} MB"


class BackgroundModal(ModalScreen[None]):
    CSS = """
    BackgroundModal { align: center middle; }
    BackgroundModal Vertical { width: 72; max-width: 90%; height: auto; max-height: 90%; border: thick $primary;
                               background: $surface; }
    BackgroundModal #background-text { padding: 0 1 1 1; }
    BackgroundModal OptionList { height: auto; border: none; }
    """
    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        each = background.memory_bytes()
        cost = f"Each uses about as much memory as this window: {size_label(each)}." if each else ""
        with Vertical():
            yield Static(
                "[b]Background spares[/b]\n"
                "Keep copies of aplite running in the background with the worlds already loaded. Opening aplite takes "
                "one over, so only the first open after a restart waits for the worlds to load; more than one helps "
                f"when opening several in a row. {cost}\n"
                "Off stops them right away (open windows keep running).", id="background-text")
            yield OptionList(*[Option(self.label(n, each), id=str(n)) for n in range(background.MAX_SPARES + 1)])

    @staticmethod
    def label(n: int, each: int | None) -> str:
        if n == 0:
            return "Off"
        text = f"{n} spare" + ("s" if n > 1 else "")
        return f"{text:<9} [dim]~{size_label(n * each)}[/]" if each else text

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        options.highlighted = min(count(), background.MAX_SPARES)
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        n = int(event.option.id)
        prefs.put(background.PREF, n)
        background.configure(n, Utils.user_path("logs", "aplite_background.txt"))
        self.notify("Background spares off" if n == 0 else f"Background spares: {n}")
        self.dismiss()
