"""Terminal launcher: pick a component, then the launcher exits and the caller runs it in this process."""
from __future__ import annotations

import logging
import sys
import typing

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Footer, Input, OptionList
from textual.widgets.option_list import Option

import Utils
from aplite import prefs
from worlds.LauncherComponents import Component, Type

GROUPS = [(Type.CLIENT, "Clients"), (Type.TOOL, "Tools"), (Type.ADJUSTER, "Adjusters"), (Type.MISC, "Misc")]


def runnable(component: Component) -> bool:
    """Whether aplite can run this component in the terminal (see __main__.run)."""
    return bool(component.func or component.script_name and (component.type == Type.CLIENT or component.cli))


def load_favorites() -> list[str]:
    """Shared with the kivy launcher."""
    return list(Utils.persistent_load().get("launcher", {}).get("favorites", []))


class LauncherApp(App[Component]):
    TITLE = "Archipelago"
    CSS = """
    OptionList { height: 1fr; }
    """
    BINDINGS = [("ctrl+q", "quit", "Quit"), ("ctrl+f", "toggle_favorite", "Favorite")]

    def __init__(self, components: typing.Sequence[Component], subtitle: str = "") -> None:
        super().__init__()
        self.components = [c for c in components if c.type != Type.HIDDEN]
        self.sub_title = subtitle
        self.favorites = load_favorites()
        self.name_width = max((len(c.display_name) for c in self.components), default=0)

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Search. Enter launches, ↑/↓ select.")
        yield OptionList()
        yield Footer()

    def on_mount(self) -> None:
        prefs.bind_theme(self)
        self.rebuild()
        self.query_one(Input).focus()

    def rebuild(self) -> None:
        search = self.query_one(Input).value.strip().casefold()
        matches = [c for c in self.components
                   if search in c.display_name.casefold() or search in (c.description or "").casefold()]
        groups = [("★ Favorites", [c for c in matches if c.display_name in self.favorites])]
        groups += [(label, [c for c in matches if c.type == type_ and c.display_name not in self.favorites])
                   for type_, label in GROUPS]

        options = self.query_one(OptionList)
        highlighted = options.highlighted_option.id if options.highlighted_option else None
        options.clear_options()
        for label, members in groups:
            if not members:
                continue
            options.add_option(Option(Text(label, style="bold"), disabled=True))
            for component in members:
                options.add_option(Option(self.render_component(component), id=component.display_name,
                                          disabled=not runnable(component)))
        ids = [option.id for option in options.options if not option.disabled]
        if ids:
            options.highlighted = options.get_option_index(highlighted if highlighted in ids else ids[0])

    def render_component(self, component: Component) -> Text:
        text = Text("  ").append(component.display_name.ljust(self.name_width + 2))
        if runnable(component):
            text.append(" ".join((component.description or "").split()), style="dim")  # some span lines
        else:
            text.append("(needs full GUI)", style="dim italic")
        return text

    def selected(self) -> Component | None:
        option = self.query_one(OptionList).highlighted_option
        if option and not option.disabled:
            return next(c for c in self.components if c.display_name == option.id)
        return None

    def on_input_changed(self) -> None:
        self.rebuild()

    def on_key(self, event) -> None:
        if event.key in ("up", "down") and isinstance(self.focused, Input):
            options = self.query_one(OptionList)
            if event.key == "up":
                options.action_cursor_up()
            else:
                options.action_cursor_down()
            event.stop()

    def on_input_submitted(self) -> None:
        self.launch()

    def on_option_list_option_selected(self) -> None:
        self.launch()

    def launch(self) -> None:
        component = self.selected()
        if component:
            self.exit(component)

    def action_toggle_favorite(self) -> None:
        component = self.selected()
        if not component:
            return
        if component.display_name in self.favorites:
            self.favorites.remove(component.display_name)
        else:
            self.favorites.append(component.display_name)
        Utils.persistent_store("launcher", "favorites", self.favorites)
        self.rebuild()


def pick(components: typing.Sequence[Component], subtitle: str = "") -> Component | None:
    """Show the launcher; returns the chosen component, or None if the user quit."""
    # log lines written to the real terminal would scribble over the app
    root = logging.getLogger()
    streams = [h for h in root.handlers if type(h) is logging.StreamHandler and h.stream in (sys.stdout, sys.stderr)]
    for handler in streams:
        root.removeHandler(handler)
    try:
        return LauncherApp(components, subtitle).run()
    finally:
        for handler in streams:
            root.addHandler(handler)
