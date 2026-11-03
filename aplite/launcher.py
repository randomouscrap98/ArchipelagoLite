"""Terminal launcher: pick a component, then the launcher exits and the caller runs it in this process."""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import typing

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Footer, Input, OptionList
from textual.widgets.option_list import Option

import Utils
from aplite import prefs
from aplite.nav import AppliteApp
from aplite.components import override_for, returns_to_launcher, runnable

if typing.TYPE_CHECKING:
    from worlds.LauncherComponents import Component  # importing worlds loads all of them, so only lazily

Components = typing.Sequence["Component"]


def load_favorites() -> list[str]:
    """Shared with the kivy launcher."""
    return list(Utils.persistent_load().get("launcher", {}).get("favorites", []))


@dataclasses.dataclass
class Cached:
    """A component as the last world load saw it: listed (not launched) while the worlds load."""
    display_name: str
    description: str
    type: str  # Type member name
    runnable: bool


def cache_file() -> str:
    return Utils.cache_path("aplite", "launcher.json")


def load_cache() -> list[Cached]:
    try:
        with open(cache_file(), encoding="utf-8") as f:
            return [Cached(**entry) for entry in json.load(f)]
    except Exception:  # missing, unreadable or from an older format: just show "Loading worlds…"
        return []


def save_cache(components: Components) -> None:
    entries = [dataclasses.asdict(Cached(c.display_name, c.description or "", c.type.name, runnable(c)))
               for c in components]
    try:
        os.makedirs(os.path.dirname(cache_file()), exist_ok=True)
        with open(cache_file() + ".tmp", "w", encoding="utf-8") as f:
            json.dump(entries, f)
        os.replace(cache_file() + ".tmp", cache_file())
    except OSError as e:
        logging.debug(f"Could not write launcher cache: {e}")


def can_run(component: Component | Cached) -> bool:
    return component.runnable if isinstance(component, Cached) else runnable(component)


def type_name(component: Component | Cached) -> str:
    return component.type if isinstance(component, Cached) else component.type.name


Choice = tuple["Component", tuple[str, ...], bool]
"""What the launcher returns: the component to run, its args, and whether to come back to the launcher after."""


class LauncherApp(AppliteApp):
    TITLE = "Archipelago"
    CSS = """
    OptionList { height: 1fr; }
    """
    BINDINGS = [("ctrl+f", "toggle_favorite", "Favorite")]

    def __init__(self, components: Components | typing.Callable[[], Components], subtitle: str = "") -> None:
        """`components` may be a function, which is run in a thread while the launcher already shows."""
        super().__init__()
        self.source = components
        self.loading = callable(components)
        self.components: list[Component | Cached] = load_cache() if self.loading else []  # Cached while loading
        self.status: str | None = "Loading worlds…"  # a line above the list (the only line if there's no list)
        self.sub_title = subtitle
        self.favorites = load_favorites()
        self.name_width = max((len(c.display_name) for c in self.components), default=0)
        self.launch_when_loaded: str | bool = False  # Enter pressed while loading: the highlighted name, or True

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Search. Enter launches, ↑/↓ select.")
        yield OptionList()
        yield Footer()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        return parameters != ("launcher",) and super().check_action(action, parameters)  # already in the launcher

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
        self.components = []
        self.set_status(f"Loading worlds failed: {error!r} (see log)")

    def set_components(self, components: Components) -> None:
        from worlds.LauncherComponents import Type
        self.components = [c for c in components if c.type != Type.HIDDEN]
        self.name_width = max((len(c.display_name) for c in self.components), default=0)
        if self.loading:
            save_cache(self.components)
        self.loading = False
        self.set_status(None)
        pending, self.launch_when_loaded = self.launch_when_loaded, False
        if isinstance(pending, str):
            options = self.query_one(OptionList)
            try:
                index = options.get_option_index(pending)
            except Exception:  # OptionDoesNotExist: gone since the cache was written
                self.notify(f"{pending} is no longer available", severity="error")
                return
            if options.get_option_at_index(index).disabled:
                return
            options.highlighted = index
            self.launch()
        elif pending:
            self.launch()

    def set_status(self, status: str | None) -> None:
        self.status = status
        self.rebuild()

    def rebuild(self) -> None:
        options = self.query_one(OptionList)
        # by Type member name: importing worlds.LauncherComponents loads all worlds, which may still be loading
        groups_by_type = [("CLIENT", "Clients"), ("TOOL", "Tools"), ("ADJUSTER", "Adjusters"), ("MISC", "Misc")]
        search = self.query_one(Input).value.strip().casefold()
        matches = [c for c in self.components
                   if search in c.display_name.casefold() or search in (c.description or "").casefold()]
        groups = [("★ Favorites", [c for c in matches if c.display_name in self.favorites])]
        groups += [(label, [c for c in matches if type_name(c) == type_ and c.display_name not in self.favorites])
                   for type_, label in groups_by_type]

        highlighted = options.highlighted_option.id if options.highlighted_option else None
        options.clear_options()
        if self.status:
            options.add_option(Option(Text(self.status, style="italic"), disabled=True))
        for label, members in groups:
            if not members:
                continue
            options.add_option(Option(Text(label, style="bold"), disabled=True))
            for component in members:
                options.add_option(Option(self.render_component(component), id=component.display_name,
                                          disabled=not can_run(component)))
        ids = [option.id for option in options.options if not option.disabled]
        if ids:
            options.highlighted = options.get_option_index(highlighted if highlighted in ids else ids[0])

    def render_component(self, component: Component | Cached) -> Text:
        text = Text("  ").append(component.display_name.ljust(self.name_width + 2))
        if can_run(component):
            text.append(" ".join((component.description or "").split()), style="dim")  # some span lines
        else:
            text.append("(needs full GUI)", style="dim italic")
        return text

    def selected(self) -> Component | Cached | None:
        option = self.query_one(OptionList).highlighted_option
        if option and not option.disabled:
            return next(c for c in self.components if c.display_name == option.id)
        return None

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """On the first entry, scroll to the very top so its group header (and the loading line) show too."""
        options = event.option_list
        first = next((i for i, option in enumerate(options.options) if not option.disabled), None)
        if event.option_index == first:
            options.call_after_refresh(options.scroll_home, animate=False)

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
            component = self.selected()
            self.launch_when_loaded = component.display_name if component else True
            self.set_status(f"Loading worlds… (will launch {component.display_name if component else 'the top match'})")
            return
        component = self.selected()
        if not component:
            return
        override = override_for(component)
        if override.picker:  # e.g. Open Patch: pick a file first, then run what it resolves to
            self.push_screen(override.picker(), lambda result: self.picked(component, result))
        else:
            self.exit((component, (), returns_to_launcher(component)))

    def picked(self, component: Component, result: typing.Any) -> None:
        if result is None:
            return  # backed out of the picker: stay in the launcher
        outcome = override_for(component).picked(component, result)
        if isinstance(outcome, str):
            self.notify(outcome, severity="error")
            return
        target, args = outcome
        self.exit((target, args, returns_to_launcher(component)))

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
    from aplite.tui import quiet_terminal
    with quiet_terminal():
        return LauncherApp(components, subtitle).run()
