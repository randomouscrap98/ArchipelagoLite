"""Terminal launcher: pick a component, then the launcher exits and the caller runs it in this process."""
from __future__ import annotations

import dataclasses
import json
import logging
import typing

from rich.padding import Padding
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

import Utils
from aplite import datadir, glyphs, write_atomic
from aplite.combo import FilterInput, first_enabled, keep_scroll
from aplite.version import version
from aplite.nav import AppliteApp
from aplite.components import RENAMED, override_for, returns_to_launcher, runnable

if typing.TYPE_CHECKING:
    from worlds.LauncherComponents import Component  # importing worlds loads all of them, so only lazily

Components = typing.Sequence["Component"]


def load_favorites() -> list[str]:
    """Shared with the kivy launcher. Favorites saved under a component's old name show under its aplite name."""
    return [RENAMED.get(name, name) for name in Utils.persistent_load().get("launcher", {}).get("favorites", [])]


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
        write_atomic(cache_file(), json.dumps(entries))
    except OSError as e:
        logging.debug(f"Could not write launcher cache: {e}")


def can_run(component: Component | Cached) -> bool:
    return component.runnable if isinstance(component, Cached) else runnable(component)


def type_name(component: Component | Cached) -> str:
    return component.type if isinstance(component, Cached) else component.type.name


Choice = tuple["Component", tuple[str, ...], bool]
"""What the launcher returns: the component to run, its args, and whether to come back to the launcher after."""


class MovedModal(ModalScreen[None]):
    """Once, after datadir moved user data out of app/ (tar.gz, Windows zip)."""
    CSS = """
    MovedModal { align: center middle; }
    #moved-box { width: 90%; height: auto; max-height: 90%; padding: 1 2; border: thick $primary; background: $surface; }
    #moved-buttons { height: auto; align-horizontal: right; margin-top: 1; }
    """
    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="moved-box"):
            yield Static(self.text, markup=False)
            with Horizontal(id="moved-buttons"):
                yield Button("OK", id="moved-ok")

    def on_mount(self) -> None:
        self.query_one(Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss()


class LauncherApp(AppliteApp):
    TITLE = "Archipelago"
    CSS = """
    #status-row { height: 1; padding: 0 2; }
    #status { width: 1fr; }
    #version { width: auto; color: $text-muted; }
    OptionList { height: 1fr; overflow-y: scroll; }
    """
    BINDINGS = [("ctrl+f", "toggle_favorite", "Favorite")]
    LAUNCHER_KEY = False  # already in the launcher

    def __init__(self, components: Components | typing.Callable[[], Components], subtitle: str = "") -> None:
        """`components` may be a function, which is run in a thread while the launcher already shows."""
        super().__init__()
        self.source = components
        self.loading = callable(components)
        self.components: list[Component | Cached] = load_cache() if self.loading else []  # Cached while loading
        self.listed: list[Component | Cached | None] = []
        """The component on each list row (None: group headings), in list order."""
        self.status: str | None = "Loading worlds…"  # the line above the list; None: it shows the entry count
        self.sub_title = subtitle
        self.favorites = load_favorites()
        self.name_width = max((len(c.display_name) for c in self.components), default=0)
        self.launch_when_loaded: str | bool = False  # Enter pressed while loading: the highlighted name, or True
        self.counts = ""
        """The entry count part of the status line ("62", "3 of 62")."""
        self.spares = ""
        """Background spares on the status line ("2/3 spares ready"), when that's on."""

    def compose(self) -> ComposeResult:
        yield FilterInput(placeholder="Search. Enter launches, ↑/↓ select.", target="OptionList")
        with Horizontal(id="status-row"):  # its own line, so the list doesn't shift when loading finishes
            yield Static(id="status")
            yield Static(f"Archipelago Lite {version()}", id="version")
        yield OptionList()
        yield Footer()

    def on_mount(self) -> None:
        if callable(self.source):
            self.run_worker(self.load_components, thread=True)
        else:
            self.set_components(self.source)
        self.rebuild()
        self.query_one(Input).focus()
        self.poll_spares()
        self.set_interval(2, self.poll_spares)
        if datadir.migration:
            self.push_screen(MovedModal(datadir.message(datadir.migration)))
            datadir.migration = None

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
            index = self.option_index(pending)
            if index is None:  # gone since the cache was written
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

    def poll_spares(self) -> None:
        from aplite import background
        if background.SUPPORTED and background.spare_count():
            self.run_worker(self.read_spares, thread=True, exclusive=True, group="spares")
        elif self.spares:
            self.spares = ""
            self.update_status()

    def read_spares(self) -> None:
        from aplite import background
        try:
            status = background.request({"op": "status"})
            ready = sum(1 for _, loaded, waiting in status["spares"] if loaded and waiting)
            text = f"{ready}/{status['count']} spares ready"
        except (OSError, EOFError, KeyError, TypeError):
            text = "spares not running"  # on in aplite.yaml, but no manager (yet, or it died)
        if text != self.spares:
            self.call_from_thread(self.show_spares, text)

    def show_spares(self, text: str) -> None:
        self.spares = text
        self.update_status()

    def update_status(self) -> None:
        status = Text()  # styles on the parts: a Text's own style would cover the spares part too
        if self.status:
            status.append(self.status, style="italic")
        else:
            status.append(f"{self.counts} components", style="dim")
        if self.spares:
            status.append(f", {self.spares}", style="dim")
        self.query_one("#status", Static).update(status)

    def rebuild(self, keep_position: bool = True) -> None:
        """Refill the list, keeping the highlighted entry; keep_position: and the scroll (not for a new search)."""
        options = self.query_one(OptionList)
        # by Type member name: importing worlds.LauncherComponents loads all worlds, which may still be loading
        groups_by_type = [("CLIENT", "Clients"), ("TOOL", "Tools"), ("ADJUSTER", "Adjusters"), ("MISC", "Misc")]
        search = self.query_one(Input).value.strip().casefold()
        matches = sorted((c for c in self.components
                          if search in c.display_name.casefold() or search in (c.description or "").casefold()),
                         key=lambda c: c.display_name.casefold())  # alphabetical in every group
        groups = [(f"{glyphs.symbol('favorite')} Favorites", [c for c in matches if c.display_name in self.favorites])]
        groups += [(label, [c for c in matches if type_name(c) == type_ and c.display_name not in self.favorites])
                   for type_, label in groups_by_type]

        self.counts = f"{len(matches)} of {len(self.components)}" if search else str(len(self.components))
        self.update_status()

        highlighted = self.listed[options.highlighted] if options.highlighted is not None else None
        highlighted = highlighted.display_name if highlighted else None
        if keep_position:
            keep_scroll(options)
        options.clear_options()
        self.listed = []
        for label, members in groups:
            if not members:
                continue
            options.add_option(Option(Text(label, style="bold"), disabled=True))
            self.listed.append(None)
            for component in members:  # no option ids: display names aren't always unique
                options.add_option(Option(self.render_component(component), disabled=not can_run(component)))
                self.listed.append(component)
        index = self.option_index(highlighted) if highlighted else None
        if index is None or options.get_option_at_index(index).disabled:
            index = first_enabled(options)
        if index is not None:
            options.highlighted = index

    def option_index(self, name: str) -> int | None:
        """List index of the first entry for the component called `name`."""
        return next((i for i, c in enumerate(self.listed) if c and c.display_name == name), None)

    def render_component(self, component: Component | Cached) -> Padding:
        if can_run(component):
            description = Text(" ".join((component.description or "").split()), style="dim")  # some span lines
        else:
            description = Text("(needs full GUI)", style="dim italic")
        # two columns, so a long description wraps under itself rather than under the names
        row = Table.grid(padding=(0, 2))
        row.add_column(width=self.name_width)
        row.add_column(ratio=1)
        row.add_row(Text(component.display_name), description)
        return Padding(row, (0, 0, 0, 2))

    def selected(self) -> Component | Cached | None:
        options = self.query_one(OptionList)
        option = options.highlighted_option
        if option and not option.disabled:
            return self.listed[options.highlighted]
        return None

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """On the first entry, scroll to the very top so its group header shows too."""
        options = event.option_list
        if event.option_index == first_enabled(options):
            options.call_after_refresh(options.scroll_home, animate=False)

    def on_input_changed(self) -> None:
        self.rebuild(keep_position=False)

    def on_input_submitted(self) -> None:
        self.launch()

    def on_option_list_option_selected(self) -> None:
        self.launch()

    def launch(self) -> None:
        component = self.selected()
        if self.loading:
            self.launch_when_loaded = component.display_name if component else True
            self.set_status(f"Loading worlds… (will launch {component.display_name if component else 'the top match'})")
            return
        if not component:
            return
        override = override_for(component)
        if override.screen:  # e.g. Documentation: nothing to run, so no fresh launcher afterwards
            self.push_screen(override.screen())
        elif override.picker:  # e.g. Open Patch: pick a file first, then run what it resolves to
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
        Utils.persistent_store("launcher", "favorites", list(self.favorites))  # copy: the store caches it
        self.rebuild()


def pick(components: Components | typing.Callable[[], Components], subtitle: str = "") -> Choice | None:
    """Show the launcher; returns the chosen component and its args, or None if the user quit.
    `components` may be a function that loads them; the launcher shows while it runs in a thread."""
    return LauncherApp(components, subtitle).run()
