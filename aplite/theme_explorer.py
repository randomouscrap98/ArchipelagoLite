"""Theme Explorer (Options menu): every client color role with its dark/light color, a preview, and a picker."""
from __future__ import annotations

import colorsys

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
HUES = 12
LIGHTNESS = (0.35, 0.5, 0.65, 0.8)


def swatch(value: str, text_color: str, custom: bool = False) -> Text:
    return Text(f" {value}{'*' if custom else ' '}", style=Style(color=text_color, bgcolor=value))


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
    rows = [[_hex(*colorsys.hls_to_rgb(hue / HUES, light, 1.0)) for hue in range(HUES)] for light in LIGHTNESS]
    rows.append([_hex(level, level, level) for level in (i / (HUES - 1) for i in range(HUES))])
    return rows


def _hex(r: float, g: float, b: float) -> str:
    return "#%02X%02X%02X" % (round(r * 255), round(g * 255), round(b * 255))


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
            f"{'dark' if theme.dark else 'light'} colors"
        self.query_one("#info", Static).update(
            f"Client colors. Theme: {theme.name}, using {mode}. Enter edits, * = customized. Themes: Ctrl+P.")
        # swatch text: dark on the dark-theme colors, light on the light-theme ones, from the current theme
        variables = app.get_css_variables()
        dark_text, light_text = variables["background"], variables["foreground"]
        if not theme.dark:
            dark_text, light_text = light_text, dark_text
        text_color = {"dark": dark_text, "light": light_text}
        table = self.query_one(DataTable)
        row = table.cursor_row
        table.clear()
        for name, role in colors.ROLES.items():
            table.add_row(Text(role.label, style=colors.style(name)),
                          *(swatch(colors.color(name, mode), text_color[mode], colors.is_custom(name, mode))
                            for mode in colors.MODES),
                          role.used_for, key=name)
        table.move_cursor(row=row, animate=False)
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


class ColorPicker(ModalScreen[bool]):
    CSS = """
    ColorPicker { align: center middle; }
    ColorPicker > Vertical { width: auto; height: auto; max-width: 100%; border: solid $primary;
                             background: $surface; padding: 0 1; }
    ColorPicker #title { text-style: bold; }
    ColorPicker DataTable { width: auto; height: auto; margin: 1 0; }
    ColorPicker .field { height: 1; width: auto; }
    ColorPicker .field Static { width: auto; }
    ColorPicker .field .label { width: 7; }
    ColorPicker Input { width: 11; }
    ColorPicker #buttons { height: auto; width: auto; margin-top: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, role: str) -> None:
        super().__init__()
        self.role = role
        self.target = "dark"
        """Field the grid writes to (the last one focused)."""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"{colors.ROLES[self.role].label}: {colors.ROLES[self.role].used_for}", id="title")
            yield Static("Enter on a color sets the field marked <-; type a hex to set your own.")
            yield DataTable(show_header=False, cursor_type="cell", id="grid")
            for mode in colors.MODES:
                with Horizontal(classes="field"):
                    yield Static(f"{mode.capitalize()}:", classes="label")
                    yield Input(colors.color(self.role, mode), id=f"input-{mode}", compact=True, max_length=7)
                    yield Static(id=f"sample-{mode}")
                    yield Static(id=f"target-{mode}")
            with Horizontal(id="buttons"):
                yield Button("Default", id="default", compact=True)
                yield Button("Cancel", id="cancel", compact=True)
                yield Button("Save", id="save", compact=True, variant="primary")

    def on_mount(self) -> None:
        grid = self.query_one("#grid", DataTable)
        rows = grid_colors()
        grid.add_columns(*([""] * HUES))
        for row in rows:
            grid.add_row(*(Text("    ", style=Style(bgcolor=value)) for value in row))
        self.target = "light" if not self.app.current_theme.dark else "dark"
        self.refresh_fields()
        grid.focus()

    def value(self, mode: str) -> str | None:
        return colors.normalize(self.query_one(f"#input-{mode}", Input).value)

    def refresh_fields(self) -> None:
        for mode in colors.MODES:
            value = self.value(mode)
            sample = Text(SAMPLE, style=f"{value} on {BACKGROUND[mode]}") if value else Text(" not a #RRGGBB color ")
            self.query_one(f"#sample-{mode}", Static).update(Text(" ").append(sample))
            self.query_one(f"#target-{mode}", Static).update(" <-" if mode == self.target else "")

    @on(Input.Changed)
    def input_changed(self) -> None:
        self.refresh_fields()

    def on_descendant_focus(self, event) -> None:
        if isinstance(event.widget, Input):
            self.target = event.widget.id.removeprefix("input-")
            self.refresh_fields()

    @on(Input.Submitted)
    def input_submitted(self, event: Input.Submitted) -> None:
        event.stop()  # Input.Submitted bubbles to the App otherwise
        self.query_one("#grid", DataTable).focus()

    @on(DataTable.CellSelected)
    def cell_selected(self, event: DataTable.CellSelected) -> None:
        row, column = event.coordinate
        self.query_one(f"#input-{self.target}", Input).value = grid_colors()[row][column]

    @on(Button.Pressed, "#default")
    def default(self) -> None:
        for mode in colors.MODES:
            self.query_one(f"#input-{mode}", Input).value = colors.default(self.role, mode)

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
