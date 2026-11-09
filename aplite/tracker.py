"""Tracker tab for Universal Tracker (community tracker.apworld) and clients embedding it: anything with
ctx.tracker_core. UT fills its kivy page through ctx.tracker_page (addLine / resetData / data / refresh_from_data) and
its summary labels through ctx.tracker_*_label.text; both are duck-typed, so aplite hands it plain objects and keeps
ctx.ui None (the map code stays off)."""
from __future__ import annotations

import re
import typing

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import OptionList, Static

from aplite import colors
from aplite.combo import FilterInput

if typing.TYPE_CHECKING:
    from CommonClient import CommonContext

# per TrackerLogLineGroup / UTTextColor name; UT's kivy colors (Tracker.kv) assume a dark background
STYLES = {"error": colors.style("tracker_out_of_logic"), "default": Style(), "ut_status": Style(italic=True),
          "hinted": colors.style("tracker_hinted"), "hinted_in_logic": colors.style("tracker_hinted_in_logic"),
          "hinted_glitched": colors.style("tracker_hinted_glitched"), "excluded": Style(dim=True),
          "excluded_glitched": colors.style("tracker_glitched") + Style(dim=True),
          "glitched": colors.style("tracker_glitched"), "unconnected": colors.style("tracker_in_logic"),
          "in_logic": colors.style("tracker_in_logic"), "out_of_logic": colors.style("tracker_out_of_logic")}
"""UT's color groups -> client color roles (colors.py)."""
NO_GUI_COLOR = "DD00FF"  # what UT's module-level get_ut_color returns without kivy: treat as unstyled
MARKUP = re.compile(r"\[color=([^\]]*)\](.*?)\[/color\]")
LABELS = ("total_locs", "logic_locs", "glitched_locs", "hinted_locs", "go_mode")


def to_text(line: str) -> Text:
    """UT's kivy markup ([color=...]...[/color]) as Rich text."""
    text, end = Text(), 0
    for match in MARKUP.finditer(line):
        text.append(line[end:match.start()])
        color = match.group(1)
        style = STYLES.get(color, "" if color.upper() == NO_GUI_COLOR else f"#{color}")
        text.append(match.group(2), style=style)
        end = match.end()
    return text.append(line[end:])


class Page:
    """Stands in for UT's kivy TrackerView. UT calls it from the client's event loop; the tab polls `version`."""
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.version = 0

    def addLine(self, line: str, sort: bool = False) -> None:  # noqa: N802 (UT's names)
        self.lines.append(line)
        self.version += 1

    def resetData(self) -> None:  # noqa: N802
        self.lines = []
        self.version += 1

    def refresh_from_data(self) -> None:
        self.version += 1

    @property
    def data(self) -> list[dict]:
        return [{"text": line} for line in self.lines]

    @data.setter
    def data(self, rows: list[dict]) -> None:
        self.lines = [row["text"] for row in rows]
        self.version += 1


class Label:
    def __init__(self) -> None:
        self.text = ""


def supported(ctx: CommonContext) -> bool:
    return hasattr(ctx, "tracker_core")


def find_nearest(lines: list, line, index: int) -> int:
    """Index of the `line` in `lines` closest to `index`, or `index` if it's gone."""
    matches = [i for i, other in enumerate(lines) if other == line]
    return min(matches, key=lambda i: abs(i - index)) if matches else index


class TrackerPane(Vertical):
    DEFAULT_CSS = """
    TrackerPane { height: 1fr; }
    TrackerPane #tracker-summary { height: auto; padding: 0 1; background: $panel; }
    TrackerPane #tracker-filter { height: 1; }
    TrackerPane #tracker-list { height: 1fr; }
    """
    # Esc in an empty filter (FilterInput skips it) goes back to the command input
    BINDINGS = [Binding("escape", "app.focus_command", show=False)]

    def __init__(self, ctx: CommonContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.page = Page()
        self.labels = {name: Label() for name in LABELS}
        self.shown: tuple | None = None
        ctx.tracker_page = self.page
        for name, label in self.labels.items():
            setattr(ctx, f"tracker_{name}_label", label)
        ctx.tracker_core.set_get_ut_color(lambda group: group)  # group names in the markup, styled by STYLES

    def compose(self) -> ComposeResult:
        yield Static(id="tracker-summary")
        yield FilterInput(placeholder="Filter locations (Up/Down scroll the list)", id="tracker-filter",
                          classes="tab-search", target="#tracker-list", compact=True)
        yield OptionList(id="tracker-list")

    def on_mount(self) -> None:
        self.refresh_lines()

    def on_input_changed(self, event: FilterInput.Changed) -> None:
        event.stop()
        self.refresh_lines()

    def refresh_lines(self) -> None:
        """Re-render when UT changed something or the filter did (called from the app's state poll)."""
        query = self.query_one("#tracker-filter", FilterInput).value.casefold()
        summary = tuple(label.text for label in self.labels.values())
        key = (self.page.version, query, summary)
        if key == self.shown:
            return
        self.shown = key
        status = self.query_one("#tracker-summary", Static)
        status.display = any(summary)
        status.update(Text("  │  ").join(to_text(text) for text in summary if text))
        lines = [to_text(line) for line in list(self.page.lines)]
        options = self.query_one("#tracker-list", OptionList)
        old = [options.get_option_at_index(i).prompt for i in range(options.option_count)]
        highlighted, scroll_y = options.highlighted, options.scroll_y
        top = min(int(scroll_y), len(old) - 1)  # assumes one line per entry (tracker lines are short)
        options.clear_options()
        options.add_options([line for line in lines if query in line.plain.casefold()]
                            or [Text("No matches." if query and lines else "Waiting for the tracker...",
                                     style="dim")])
        # Keep the user's place when entries appear/disappear above it: the highlight and the top visible line follow
        # their entry's text (nearest match, names can repeat in headers), else stay at the same index
        new = [options.get_option_at_index(i).prompt for i in range(options.option_count)]
        if highlighted is not None and highlighted < len(old):
            highlighted = find_nearest(new, old[highlighted], highlighted)
            if highlighted < options.option_count:
                options.highlighted = highlighted
        if top >= 0:
            scroll_y += find_nearest(new, old[top], top) - top
        # clearing reset the scroll, and restoring the highlight scrolls to it
        options.call_after_refresh(options.scroll_to, y=scroll_y, animate=False)
