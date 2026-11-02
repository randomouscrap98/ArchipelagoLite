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

if typing.TYPE_CHECKING:
    from worlds.LauncherComponents import Component  # importing worlds loads all of them, so only lazily

Components = typing.Sequence["Component"]


def is_options_creator(component: Component) -> bool:
    """Upstream's is kivy; aplite runs its own (aplite/options_creator.py)."""
    return component.script_name == "OptionsCreator"


def runnable(component: Component) -> bool:
    """Whether aplite can run this component in the terminal (see __main__.run)."""
    from worlds.LauncherComponents import Type
    return bool(component.func or is_options_creator(component) or
                component.script_name and (component.type == Type.CLIENT or component.cli))


def load_favorites() -> list[str]:
    """Shared with the kivy launcher."""
    return list(Utils.persistent_load().get("launcher", {}).get("favorites", []))


Choice = tuple["Component", tuple[str, ...], bool]
"""What the launcher returns: the component to run, its args, and whether to come back to the launcher after."""


class LauncherApp(App[Choice]):
    TITLE = "Archipelago"
    CSS = """
    OptionList { height: 1fr; }
    """
    BINDINGS = [("ctrl+q", "quit", "Quit"), ("ctrl+f", "toggle_favorite", "Favorite")]

    def __init__(self, components: Components | typing.Callable[[], Components], subtitle: str = "") -> None:
        """`components` may be a function, which is run in a thread while the launcher already shows."""
        super().__init__()
        self.source = components
        self.components: list[Component] = []
        self.loading = callable(components)
        self.status: str | None = "Loading worlds…"
        self.sub_title = subtitle
        self.favorites = load_favorites()
        self.name_width = 0
        self.launch_when_loaded = False  # Enter pressed while still loading

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Search. Enter launches, ↑/↓ select.")
        yield OptionList()
        yield Footer()

    def on_mount(self) -> None:
        prefs.bind_theme(self)
        if callable(self.source):
            self.run_worker(self.load_components, thread=True)
        else:
            self.set_components(self.source)
        self.rebuild()
        self.query_one(Input).focus()

    def load_components(self) -> None:
        try:
            components = self.source()
        except BaseException as e:  # SystemExit too: a world calling sys.exit while loading
            logging.exception("Loading worlds failed")
            self.call_from_thread(self.load_failed, e)
        else:
            self.call_from_thread(self.set_components, components)

    def load_failed(self, error: BaseException) -> None:
        self.loading = False
        self.set_status(f"Loading worlds failed: {error!r} (see log)")

    def set_components(self, components: Components) -> None:
        from worlds.LauncherComponents import Type
        self.components = [c for c in components if c.type != Type.HIDDEN]
        self.name_width = max((len(c.display_name) for c in self.components), default=0)
        self.loading = False
        self.set_status(None)
        if self.launch_when_loaded:
            self.launch()

    def set_status(self, status: str | None) -> None:
        self.status = status
        self.rebuild()

    def rebuild(self) -> None:
        options = self.query_one(OptionList)
        if self.status:
            options.clear_options()
            options.add_option(Option(Text(self.status, style="italic"), disabled=True))
            return
        from worlds.LauncherComponents import Type
        groups_by_type = [(Type.CLIENT, "Clients"), (Type.TOOL, "Tools"), (Type.ADJUSTER, "Adjusters"),
                          (Type.MISC, "Misc")]
        search = self.query_one(Input).value.strip().casefold()
        matches = [c for c in self.components
                   if search in c.display_name.casefold() or search in (c.description or "").casefold()]
        groups = [("★ Favorites", [c for c in matches if c.display_name in self.favorites])]
        groups += [(label, [c for c in matches if c.type == type_ and c.display_name not in self.favorites])
                   for type_, label in groups_by_type]

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
        if self.loading:
            self.launch_when_loaded = True
            self.set_status("Loading worlds… (will launch the top match)")
            return
        component = self.selected()
        if not component:
            return
        from worlds.LauncherComponents import open_patch
        if component.func is open_patch:  # upstream's opens a native dialog, then a new kivy launcher process
            from aplite.patch_picker import PatchPicker
            self.push_screen(PatchPicker(), self.patch_chosen)
        else:
            self.exit((component, (), is_options_creator(component)))

    def patch_chosen(self, path) -> None:
        if path is None:
            return  # back to the launcher
        from worlds.LauncherComponents import identify
        file, component = identify(str(path))
        if component and runnable(component):
            self.exit((component, (file,), True))
        else:
            self.notify(f"No client can open {path.name}", severity="error")

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


def pick(components: Components | typing.Callable[[], Components], subtitle: str = "") -> Choice | None:
    """Show the launcher; returns the chosen component and its args, or None if the user quit.
    `components` may be a function that loads them; the launcher shows while it runs in a thread."""
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
