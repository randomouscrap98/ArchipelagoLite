"""Theme Explorer (Options menu): every client color role with its dark/light color, a preview, and a picker."""
from __future__ import annotations

from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Input, Static

from aplite import colors

BACKGROUND = {"dark": "#0C0C0C", "light": "#F7F7F7"}
"""Picker preview backgrounds (Textual's dark / light ANSI palette backgrounds)."""
SAMPLE = " Sample Text "
ENDESGA_64 = (
    "FF0040 131313 1B1B1B 272727 3D3D3D 5D5D5D 858585 B4B4B4",
    "FFFFFF C7CFDD 92A1B9 657392 424C6E 2A2F4E 1A1932 0E071B",
    "1C121C 391F21 5D2C28 8A4836 BF6F4A E69C69 F6CA9F F9E6CF",
    "EDAB50 E07438 C64524 8E251D FF5000 ED7614 FFA214 FFC825",
    "FFEB57 D3FC7E 99E65F 5AC54F 33984B 1E6F50 134C4C 0C2E44",
    "00396D 0069AA 0098DC 00CDF9 0CF1FF 94FDFF FDD2ED F389F5",
    "DB3FFD 7A09FA 3003D9 0C0293 03193F 3B1443 622461 93388F",
    "CA52C9 C85086 F68187 F5555D EA323C C42430 891E2B 571C27",
)
BLK_NX64 = (
    "000000 12173D 293268 464B8C 6B74B2 909EDD C1D9F2 E3EFFA",
    "A293C4 7B6AA5 53427F 3C2C68 431E66 5D2F8C 854CBF B483EF",
    "8CFF9B 42BC7F 22896E 14665B 0F4A4C 0A2A33 1D1A59 322D89",
    "354AB2 3E83D1 50B9EB 8CDAFF 53A1AD 3B768F 21526B 163755",
    "008782 00AAA5 27D3CB 78FAE6 CDC599 988F64 5C5D41 353F23",
    "919B45 AFD370 FFE091 FFAA6E FF695A B23C40 FF6675 DD3745",
    "A52639 721C2F B22E69 E54286 FF6EAF FFA5D5 FFD3AD CC817A",
    "895654 61393B 3F1F3C 723352 994C69 C37289 F29FAA FFCCD0",
)
"""Picker palettes (Endesga 64, BLK NX64), 8 colors per line as published: shown side by side, line for line.
NX64's FFFFFF (also in Endesga) is E3EFFA here, continuing its blue ramp."""


def swatch(value: str, custom: bool = False) -> Text:
    return Text(f" {value}{'*' if custom else ' '}", style=Style(color=_contrast(value), bgcolor=value))


def marked(*parts: str | tuple[str, str]) -> Text:
    """A line with (text, role) parts in role markers, like the client's messages (RoleFilter colors them)."""
    text = Text()
    for part in parts:
        if isinstance(part, tuple):
            text.append(part[0], style=colors.style(part[1]))
        else:
            text.append(part)
    return text


def preview() -> Text:
    lines = [
        marked(("Alice", "player_self"), " sent ", ("Hookshot", "item_progression"), " to ",
               ("Bob", "player_other"), " (", ("Kakariko Well", "location"), ")"),
        marked(("Bob", "player_other"), " found their ", ("Piece of Heart", "item_useful"), " (",
               ("Desert Chest", "location"), ")"),
        marked(("Bob", "player_other"), " sent ", ("Ice Trap", "item_trap"), " to ", ("Alice", "player_self"),
               " (", ("Lake Shop", "location"), ")"),
        marked(("Alice", "player_self"), " found their ", ("Rupees (5)", "item_filler"), " (",
               ("Lost Woods", "location"), ")"),
        marked("[Hint]: ", ("Alice", "player_self"), "'s ", ("Bow", "item_progression"), " is at ",
               ("Ganon's Tower", "location"), " in ", ("Bob", "player_other"), "'s World at ",
               ("Death Mountain", "entrance"), ". ", ("(priority)", "hint_priority")),
        marked("Statuses: ", ("(found)", "hint_found"), " ", ("(unspecified)", "hint_unspecified"), " ",
               ("(no priority)", "hint_no_priority"), " ", ("(avoid)", "hint_avoid"), " ",
               ("(priority)", "hint_priority")),
    ]
    return Text("\n").join(lines)


def grid_colors() -> list[list[str]]:
    return [[f"#{value}" for value in (left + " " + right).split()] for left, right in zip(ENDESGA_64, BLK_NX64)]


class ThemeExplorer(Screen):
    CSS = """
    ThemeExplorer #info { padding: 0 1; height: auto; }
    ThemeExplorer DataTable { height: 1fr; }
    /* Textual's unfocused highlight even when focused: colored role names stay readable on it */
    ThemeExplorer DataTable:focus > .datatable--cursor { background: $block-cursor-blurred-background;
        color: $block-cursor-blurred-foreground; }
    ThemeExplorer #preview { height: auto; padding: 1 2; border-top: solid $panel; }
    """
    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), Binding("r", "reset", "Reset role"),
                Binding("R", "reset_all", "Reset all")]

    def compose(self) -> ComposeResult:
        yield Static(id="info")
        # the highlight leaves cells' own colors alone (swatches, role names)
        yield DataTable(cursor_type="row", zebra_stripes=True, cursor_foreground_priority="renderable",
                        cursor_background_priority="renderable")
        yield Static(id="preview")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("Role", "Dark themes", "Light themes", "Used for")
        self.fill()
        self.app.theme_changed_signal.subscribe(self, lambda _: self.fill())

    def fill(self) -> None:
        app = self.app
        theme = app.current_theme
        mode = "terminal palette (ANSI theme): role colors don't apply" if app.native_ansi_color else \
            f"{colors.theme_mode(app)} colors"
        self.query_one("#info", Static).update(
            f"Client colors. Theme: {theme.name}, using {mode}. Enter edits, * = customized. Themes: Ctrl+P.")
        table = self.query_one(DataTable)
        row, scroll_y = table.cursor_row, table.scroll_y
        table.clear()
        for name, role in colors.ROLES.items():
            table.add_row(Text(role.label, style=colors.style(name)),
                          *(swatch(colors.color(name, mode), colors.is_custom(name, mode))
                            for mode in colors.MODES),
                          role.used_for, key=name)
        table.move_cursor(row=row, animate=False)
        table.call_after_refresh(table.scroll_to, y=scroll_y, animate=False)  # move_cursor scrolls it into view
        self.query_one("#preview", Static).update(preview())

    def selected(self) -> str:
        table = self.query_one(DataTable)
        return list(colors.ROLES)[table.cursor_row]

    def changed(self) -> None:
        colors.refresh(self.app)
        self.fill()

    @on(DataTable.RowSelected)
    def edit(self) -> None:
        self.app.push_screen(ColorPicker(self.selected()), lambda saved: saved and self.changed())

    def action_reset(self) -> None:
        colors.save(self.selected(), {mode: None for mode in colors.MODES})
        self.changed()

    def action_reset_all(self) -> None:
        colors.reset_all()
        self.changed()
        self.notify("All colors reset to defaults.")


class Swatches(Static):
    """The picker's color grid. Never focused: arrow keys in a hex field move its selection (combobox-style)."""
    CELL = 4

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.rows = grid_colors()
        self.position: tuple[int, int] | None = None
        """(row, column) of the selected color; None when the field's color isn't in the grid."""
        self.last = (0, 0)

    def render(self) -> Text:
        text = Text()
        for y, row in enumerate(self.rows):
            if y:
                text.append("\n")
            for x, value in enumerate(row):
                if (y, x) == self.position:
                    text.append("[  ]", style=Style(color=_contrast(value), bgcolor=value, bold=True))
                else:
                    text.append(" " * self.CELL, style=Style(bgcolor=value))
        return text

    def show(self, value: str | None) -> None:
        """Select the cell holding `value`, if any."""
        self.position = next(((y, x) for y, row in enumerate(self.rows) for x, cell in enumerate(row)
                              if cell == value), None)
        if self.position:
            self.last = self.position
        self.refresh()

    def move(self, dx: int, dy: int) -> str:
        """Move the selection (from an unmatched color: select the last cell first) and return its color."""
        if self.position is None:
            y, x = self.last
        else:
            y, x = self.position
            y = max(0, min(len(self.rows) - 1, y + dy))
            x = max(0, min(len(self.rows[0]) - 1, x + dx))
        self.position = self.last = (y, x)
        self.refresh()
        return self.rows[y][x]

    def on_click(self, event) -> None:
        y, x = event.y, event.x // self.CELL
        if 0 <= y < len(self.rows) and 0 <= x < len(self.rows[0]):
            self.position = (y, x)
            self.screen.grid_picked(self.move(0, 0))


def _contrast(value: str) -> str:
    r, g, b = (int(value[i:i + 2], 16) for i in (1, 3, 5))
    return "#000000" if 0.299 * r + 0.587 * g + 0.114 * b > 140 else "#FFFFFF"


class HexInput(Input):
    """A hex field: arrow keys pick from the grid instead of moving the text cursor."""
    BINDINGS = [Binding(key, f"grid({dx}, {dy})", show=False)
                for key, dx, dy in (("left", -1, 0), ("right", 1, 0), ("up", 0, -1), ("down", 0, 1))]

    def action_grid(self, dx: int, dy: int) -> None:
        self.value = self.screen.query_one(Swatches).move(dx, dy)
        self.cursor_position = len(self.value)


def color_note(color_system: str | None) -> str:
    """A warning when the terminal can't show the colors as picked (Rich's detection: COLORTERM, TERM,
    TEXTUAL_COLOR_SYSTEM): measured, the 128 grid colors become 86 distinct ones with 256 colors, 15 with 16."""
    if color_system == "256":
        return "Your terminal shows 256 colors: swatches are approximate. The hex value is saved exactly."
    if color_system in ("standard", "windows"):
        return "Your terminal shows 16 colors: swatches are only a rough guide. The hex value is saved exactly."
    return ""


class ColorPicker(ModalScreen[bool]):
    CSS = """
    ColorPicker { align: center middle; }
    ColorPicker > Vertical { width: auto; height: auto; max-width: 100%; border: solid $primary;
                             background: $surface; padding: 0 1; }
    ColorPicker #title { text-style: bold; }
    ColorPicker #color-note { color: $warning; width: auto; max-width: 60; }
    ColorPicker .field { height: 1; width: auto; }
    ColorPicker .field Static { width: auto; }
    ColorPicker .field .label { width: 7; }
    ColorPicker HexInput { width: 9; }
    ColorPicker HexInput:focus { background: $block-cursor-background; color: $block-cursor-foreground; }
    ColorPicker Swatches { width: auto; height: auto; margin: 1 0; }
    ColorPicker #buttons { height: auto; width: auto; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, role: str) -> None:
        super().__init__()
        self.role = role

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"{colors.ROLES[self.role].label}: {colors.ROLES[self.role].used_for}", id="title")
            yield Static("Arrow keys pick a color, or type a hex.")
            if note := color_note(self.app.console.color_system):
                yield Static(note, id="color-note")
            for mode in colors.MODES:
                with Horizontal(classes="field"):
                    yield Static(f"{mode.capitalize()}:", classes="label")
                    yield HexInput(colors.color(self.role, mode), id=f"input-{mode}", compact=True, max_length=7)
                    yield Static(id=f"sample-{mode}")
            yield Swatches()
            with Horizontal(id="buttons"):
                yield Button("Default", id="default", compact=True)
                yield Button("Cancel", id="cancel", compact=True)
                yield Button("Save", id="save", compact=True, variant="primary")

    def on_mount(self) -> None:
        self.refresh_samples()
        self.query_one(f"#input-{colors.theme_mode(self.app)}", HexInput).focus()

    def value(self, mode: str) -> str | None:
        return colors.normalize(self.query_one(f"#input-{mode}", HexInput).value)

    def focused_mode(self) -> str | None:
        focused = self.focused
        return focused.id.removeprefix("input-") if isinstance(focused, HexInput) else None

    def refresh_samples(self) -> None:
        for mode in colors.MODES:
            value = self.value(mode)
            sample = Text(SAMPLE, style=f"{value} on {BACKGROUND[mode]}") if value else Text(" not a #RRGGBB color ")
            self.query_one(f"#sample-{mode}", Static).update(Text(" ").append(sample))

    @on(Input.Changed)
    def input_changed(self, event: Input.Changed) -> None:
        self.refresh_samples()
        if event.input is self.focused:
            self.query_one(Swatches).show(self.value(self.focused_mode()))

    def on_descendant_focus(self, event) -> None:
        if isinstance(event.widget, HexInput):
            self.query_one(Swatches).show(self.value(self.focused_mode()))

    def grid_picked(self, value: str) -> None:
        mode = self.focused_mode() or colors.theme_mode(self.app)
        field = self.query_one(f"#input-{mode}", HexInput)
        field.value = value
        field.focus()

    @on(Input.Submitted)
    def input_submitted(self, event: Input.Submitted) -> None:
        event.stop()  # Input.Submitted bubbles to the App otherwise

    @on(Button.Pressed, "#default")
    def default(self) -> None:
        for mode in colors.MODES:
            self.query_one(f"#input-{mode}", HexInput).value = colors.default(self.role, mode)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        values = {mode: self.value(mode) for mode in colors.MODES}
        if None in values.values():
            self.notify("Colors must look like #RRGGBB.", severity="error")
            return
        colors.save(self.role, values)
        self.dismiss(True)
