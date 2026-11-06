"""Install/Update Apworlds: a Textual face for the APWorld Manager apworld (silasary). Its own UIs are kivy (off in
aplite) and curses (needs utils.curses_utils, newer than our base), so without this it prints a notice and exits.
Uses the world's own API, as its curses.py does: repositories, refresh_apworld_table, install_world. Research:
ignore/claude/research/apworld-manager.md."""
from __future__ import annotations

import contextlib
import typing

import Utils
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, OptionList, Static
from textual.widgets.option_list import Option

from aplite import nav
from aplite.combo import FilterInput, highlight_first

CUSTOM_REPO = "Custom repo available"
"""The description world_manager gives a row whose world names its own repository (adding it, not installing)."""


def manager_module():
    """The apworld's world_manager module, or None when the apworld isn't installed."""
    try:
        from worlds.apworld_manager import world_manager
    except ImportError:
        return None
    return world_manager


def launch_func() -> typing.Callable | None:
    """The apworld's component func, the key for aplite's override."""
    try:
        from worlds.apworld_manager import launch_client
    except ImportError:
        return None
    return launch_client


@contextlib.contextmanager
def captured_messageboxes(messages: list[tuple[str, bool]]) -> typing.Iterator[None]:
    """Upstream's install_apworld reports through Utils.messagebox (a desktop dialog): collect the texts instead."""
    original = Utils.messagebox
    Utils.messagebox = lambda title, text, error=False: messages.append((text, error))
    try:
        yield
    finally:
        Utils.messagebox = original


class ManagerApp(nav.AppliteApp):
    TITLE = "Install/Update Apworlds"
    BINDINGS = [Binding("ctrl+r", "reload", "Refresh")]
    CSS = """
    #details { height: auto; max-height: 6; padding: 0 1; border-top: solid $primary; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.manager = manager_module()
        self.worlds: list[dict] = []
        self.shown: list[dict] = []
        self.busy = False
        self.installed_any = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield FilterInput(placeholder="Search. Enter installs/updates, ↑/↓ select.", target="OptionList")
        yield OptionList()
        yield Static(id="details")
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.load()

    def status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def action_reload(self) -> None:
        if not self.busy:
            self.load()

    @work(thread=True, exclusive=True)
    def load(self) -> None:
        self.busy = True
        nav.on_app_thread(self, self.status, "Fetching repositories...")
        try:
            repos = self.manager.repositories
            repos.load_repos_from_settings()
            repos.refresh()
            worlds = self.manager.refresh_apworld_table()
        except Exception as e:  # network, a repo with bad data, or a changed world_manager API
            nav.on_app_thread(self, self.status, f"Couldn't load the world list: {e}")
            return
        finally:
            self.busy = False
        nav.on_app_thread(self, self.loaded, worlds)

    def loaded(self, worlds: list[dict]) -> None:
        self.worlds = worlds
        self.fill()
        updates = sum(w["sort"] == self.manager.SortStages.UPDATE_AVAILABLE for w in worlds)
        self.status(f"{len(worlds)} worlds, {updates} with updates. Ctrl+R refreshes.")

    def on_input_changed(self, event: FilterInput.Changed) -> None:
        self.fill()

    def fill(self) -> None:
        text = self.query_one(FilterInput).value.lower()
        self.shown = [w for w in self.worlds if text in w["title"].lower() or text in w["description"].lower()]
        options = self.query_one(OptionList)
        options.clear_options()
        width = max((len(w["title"]) for w in self.shown), default=0)
        options.add_options(Option(f"{w['title']:<{width}}  {w['install_text']}") for w in self.shown)
        highlight_first(options)
        self.show_details()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.show_details()

    def highlighted(self) -> dict | None:
        index = self.query_one(OptionList).highlighted
        return self.shown[index] if index is not None and index < len(self.shown) else None

    def show_details(self) -> None:
        world = self.highlighted()
        text = ""
        if world:
            latest = world.get("latest_version")
            version = f"  (latest {latest.world_version})" if latest else ""
            text = f"[b]{world['title']}[/b]{version}\n{world['description']}"
        self.query_one("#details", Static).update(text)

    def on_input_submitted(self, event: FilterInput.Submitted) -> None:
        event.stop()
        world = self.highlighted()
        if world is None or self.busy:
            return
        if world["install_text"] == "-" or not world.get("latest_version") and world["description"] != CUSTOM_REPO:
            self.status(f"Nothing to install for {world['title']}.")
            return
        self.install(world)

    @work(thread=True, exclusive=True)
    def install(self, world: dict) -> None:
        self.busy = True
        messages: list[tuple[str, bool]] = []
        try:
            if world["description"] == CUSTOM_REPO:  # what the kivy app does: enable the world's own repository
                from worlds.apworld_manager import RepoWorld
                manifest = world["manifest"]
                url = manifest.get("repo_url") or manifest.get("github")
                nav.on_app_thread(self, self.status, f"Adding repository {url}...")
                RepoWorld.settings.repositories[url] = True
                RepoWorld.settings._changed = True
                Utils.get_settings().save()  # aplite returns to the launcher by exec: no atexit autosave
                self.manager.repositories.add_repo(url)
                self.manager.repositories.refresh()
                done = f"Added repository {url}."
            else:
                nav.on_app_thread(self, self.status, f"Installing {world['title']}...")
                with captured_messageboxes(messages):
                    self.manager.install_world(world)
                errors = [text for text, error in messages if error]
                if errors:
                    nav.on_app_thread(self, self.status, f"Install failed: {errors[0]}")
                    return
                self.installed_any = True
                done = f"Installed {world['title']}. New worlds load next time the launcher starts."
            worlds = self.manager.refresh_apworld_table()
        except Exception as e:
            nav.on_app_thread(self, self.status, f"Install failed: {e}")
            return
        finally:
            self.busy = False
        nav.on_app_thread(self, self.loaded, worlds)
        nav.on_app_thread(self, self.status, done)


def run(*args: str) -> None:
    """Command-line args (--update-all, --install NAME) go to the apworld's own launch: they need no UI."""
    manager = manager_module()
    if manager is None:
        print("The APWorld Manager apworld isn't installed.")
        return
    if args:
        from worlds.apworld_manager import kv_app
        kv_app.launch(*args)
        return
    ManagerApp().run()
    manager.repositories.cleanup_downloads()
