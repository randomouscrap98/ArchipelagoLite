"""Install/Update Apworlds: a Textual face for the APWorld Manager apworld (silasary). Its own UIs are kivy (off in
aplite) and curses (needs utils.curses_utils, newer than our base), so without this it prints a notice and exits.
Uses the world's own API, as its curses.py does: repositories, refresh_apworld_table, install_world. Research:
ignore/claude/research/apworld-manager.md."""
from __future__ import annotations

import contextlib
import os
import re
import time
import typing

import Utils
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, OptionList, Static
from textual.widgets.option_list import Option

from aplite import nav
from aplite.master_detail import CONFIRM_SECONDS
from aplite.combo import FilterInput, highlight_first, keep_scroll
from aplite.desktop import open_path

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


def repo_url(world: dict) -> str | None:
    """Where a world lives: the manifest's repo_url/github, else the repo of a GitHub release download (nearly every
    index entry), else the folder the download comes from. The index has no repo field, and the apworld's own
    release_url is built from the index's address for index worlds (not a github.com link, so None)."""
    manifest = world.get("manifest") or {}
    if url := manifest.get("repo_url") or manifest.get("github"):
        return url
    remote = world.get("latest_version") or next(iter((world.get("remotes") or {}).values()), None)
    download = remote.data.get("world") if remote else None
    if not download:
        return None
    if match := re.match(r"(https?://[^/]+/[^/]+/[^/]+)/releases/download/", download):
        return match.group(1)
    return download.rsplit("/", 1)[0]


class VersionPicker(ModalScreen):
    """Every listed version of one world, newest first. Enter installs; an older one than installed needs a second
    Enter within CONFIRM_SECONDS. Dismisses with the chosen ApWorldMetadata, or None."""
    CSS = """
    VersionPicker { align: center middle; }
    VersionPicker Vertical { width: 70; max-width: 90%; height: auto; max-height: 80%; border: thick $primary;
                             background: $surface; }
    VersionPicker #picker-title { padding: 0 1; }
    VersionPicker OptionList { height: auto; max-height: 30; border: none; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, versions: list, installed, parse) -> None:
        """versions: ApWorldMetadata newest first; installed: the installed version (parsed) or None."""
        super().__init__()
        self.title_text = title
        self.versions = versions
        self.installed = installed
        self.parse = parse
        self.armed: tuple[int, float] | None = None
        """The older version a first Enter picked (index, time); a second Enter on it within CONFIRM_SECONDS installs."""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"[b]{self.title_text}[/b]: pick a version", id="picker-title")
            width = max(len(v.world_version) for v in self.versions)
            yield OptionList(*[Option(self.label(v, width)) for v in self.versions])

    def label(self, version, width: int) -> str:
        date = (version.created_at or "")[:10]
        parts = [f"{version.world_version:<{width}}", f"{date:<10}", f"{size_label(version.data.get('size')):>8}"]
        if version.is_prerelease or self.parse(version.world_version).is_prerelease:  # flagged, or a1/b2/rc0/dev
            parts.append("[$warning]prerelease[/]")
        if self.installed is not None and self.parse(version.world_version) == self.installed:
            parts.append("[$success]installed[/]")
        return "  ".join(parts)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        index = event.option_index
        version = self.versions[index]
        older = self.installed is not None and self.parse(version.world_version) < self.installed
        if older and not (self.armed and self.armed[0] == index and time.monotonic() - self.armed[1] < CONFIRM_SECONDS):
            self.armed = (index, time.monotonic())
            self.notify(f"{version.world_version} is older than the installed version: a downgrade. "
                        f"Press Enter again to install it.", severity="warning", timeout=CONFIRM_SECONDS)
            return
        self.dismiss(version)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ManagerApp(nav.AppliteApp):
    TITLE = "APWorld Manager"
    BINDINGS = [Binding("ctrl+r", "reload", "Refresh"), Binding("ctrl+t", "delete", "Delete"),
                Binding("ctrl+n", "versions", "Versions"), Binding("ctrl+o", "open_repo", "Open repo")]
    CSS = """
    OptionList { height: 1fr; overflow-y: scroll; }
    #details { height: auto; max-height: 6; padding: 0 1; border-top: solid $primary; }
    #details Horizontal { height: auto; }
    #heading { width: 1fr; }
    #size { width: auto; padding-left: 2; }
    #link { color: $text-muted; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.manager = manager_module()
        self.worlds: list[dict] = []
        self.shown: list[dict] = []
        self.busy = False
        self.installed_any = False
        self.armed: dict | None = None
        """The world a first ctrl+t asked to delete; a second one within CONFIRM_SECONDS deletes it."""

    def world_table(self) -> list[dict]:
        """refresh_apworld_table, with the latest version of uninstalled worlds picked by full version: it compares only
        major.minor.micro there, so ties (0.1.9a0 vs 0.1.9.post3) went to whichever the index lists last (Balatro
        installed a 2024 release). Installed worlds already compare full versions."""
        worlds = self.manager.refresh_apworld_table()
        parse = self.manager.parse_version
        for world in worlds:
            remotes = self.manager.repositories.packages_by_id_version.get(world["id"])
            if not world["installed"] and world.get("latest_version") and remotes:
                world["latest_version"] = max(remotes.values(), key=lambda r: parse(r.world_version))
        return worlds

    def compose(self) -> ComposeResult:
        yield Header()
        yield FilterInput(placeholder="Search. Enter installs/updates, ↑/↓ select.", target="OptionList")
        yield OptionList()
        with Vertical(id="details"):
            with Horizontal():
                yield Static(id="heading")
                yield Static(id="size")
            yield Static(id="link")
            yield Static(id="description")
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
            worlds = self.world_table()
        except Exception as e:  # network, a repo with bad data, or a changed world_manager API
            nav.on_app_thread(self, self.status, f"Couldn't load the world list: {e}")
            return
        finally:
            self.busy = False
        nav.on_app_thread(self, self.loaded, worlds)

    def loaded(self, worlds: list[dict]) -> None:
        self.worlds = worlds
        self.fill(keep_position=True)
        updates = sum(w["sort"] == self.manager.SortStages.UPDATE_AVAILABLE for w in worlds)
        self.status(f"{len(worlds)} worlds, {updates} with updates. Ctrl+R refreshes.")

    def on_input_changed(self, event: FilterInput.Changed) -> None:
        self.fill()

    def fill(self, keep_position: bool = False) -> None:
        """Refill the list; keep_position: same world (or its row, when it's gone) and scroll, not for a new search."""
        options = self.query_one(OptionList)
        old, old_index = self.highlighted(), options.highlighted
        text = self.query_one(FilterInput).value.lower()
        self.shown = [w for w in self.worlds if text in w["title"].lower() or text in w["description"].lower()]
        if keep_position:
            keep_scroll(options)
        options.clear_options()
        width = max((len(w["title"]) for w in self.shown), default=0)
        options.add_options(Option(f"{w['title']:<{width}}  {w['install_text']}") for w in self.shown)
        ids = [w["id"] for w in self.shown]
        if keep_position and old_index is not None and ids:
            options.highlighted = ids.index(old["id"]) if old and old["id"] in ids else min(old_index, len(ids) - 1)
        else:
            highlight_first(options)
        self.show_details()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.show_details()

    def highlighted(self) -> dict | None:
        index = self.query_one(OptionList).highlighted
        return self.shown[index] if index is not None and index < len(self.shown) else None

    def show_details(self) -> None:
        world = self.highlighted()
        heading = size = description = link = ""
        if world:
            latest = world.get("latest_version")
            version = f"  (latest {latest.world_version})" if latest else ""
            if world["installed"]:
                version = f"  installed {world['installed_version']}{version}"
                size = size_text(world.get("file"))
            heading = f"[b]{world['title']}[/b]{version}"
            description = world["description"]
            if url := repo_url(world):
                link = f'[link="{url}"]{url.removeprefix("https://")}[/link]  (Ctrl+O opens)'
        self.query_one("#heading", Static).update(heading)
        self.query_one("#link", Static).update(link)
        self.query_one("#link", Static).display = bool(link)
        self.query_one("#size", Static).update(size)
        self.query_one("#description", Static).update(description)

    def on_input_submitted(self, event: FilterInput.Submitted) -> None:
        event.stop()
        world = self.highlighted()
        if world is None or self.busy:
            return
        if world["install_text"] == "-" or not world.get("latest_version") and world["description"] != CUSTOM_REPO:
            self.status(f"Nothing to install for {world['title']}.")
            return
        self.install(world)

    def action_delete(self) -> None:
        world = self.highlighted()
        if world is None or self.busy:
            return
        file = world.get("file")
        bundled = world["sort"] in (self.manager.SortStages.BUNDLED, self.manager.SortStages.BUNDLED_BUT_UPDATABLE)
        if not world["installed"] or not file or bundled or not os.path.isfile(file):
            self.status(f"{world['title']} isn't an installed .apworld file aplite can delete.")
            return
        if self.armed is not world:
            self.armed = world
            self.notify(f"Press Ctrl+T again to delete {os.path.basename(file)}.", severity="warning",
                        timeout=CONFIRM_SECONDS)
            self.set_timer(CONFIRM_SECONDS, lambda: setattr(self, "armed", None) if self.armed is world else None)
            return
        self.armed = None
        try:
            os.remove(file)
        except OSError as e:
            self.status(f"Couldn't delete {file}: {e}")
            return
        self.worlds.remove(world)
        self.fill(keep_position=True)
        self.status(f"Deleted {file}. It stays loaded (and not installable here) until aplite restarts.")

    def action_open_repo(self) -> None:
        world = self.highlighted()
        url = repo_url(world) if world else None
        if not url:
            self.status("No repository link for this world.")
        elif not open_path(url):
            self.status(f"No desktop browser to open it: {url}")

    def action_versions(self) -> None:
        world = self.highlighted()
        if world is None or self.busy:
            return
        parse = self.manager.parse_version
        remotes = self.manager.repositories.packages_by_id_version.get(world["id"]) or {}
        if not remotes:
            self.status(f"No versions listed for {world['title']}.")
            return
        versions = sorted(remotes.values(), key=lambda r: parse(r.world_version), reverse=True)
        installed = parse(world["installed_version"]) if world["installed"] and world.get("installed_version") else None

        def picked(version) -> None:
            if version is not None:
                self.install({**world, "latest_version": version}, version.world_version)
        self.push_screen(VersionPicker(world["title"], versions, installed, parse), picked)

    @work(thread=True, exclusive=True)
    def install(self, world: dict, version: str | None = None) -> None:
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
                name = f"{world['title']} {version}" if version else world["title"]
                nav.on_app_thread(self, self.status, f"Installing {name}...")
                with captured_messageboxes(messages):
                    self.manager.install_world(world)
                errors = [text for text, error in messages if error]
                if errors:
                    nav.on_app_thread(self, self.status, f"Install failed: {errors[0]}")
                    return
                self.installed_any = True
                done = f"Installed {name}. New worlds load next time the launcher starts."
            worlds = self.world_table()
        except Exception as e:
            nav.on_app_thread(self, self.status, f"Install failed: {e}")
            return
        finally:
            self.busy = False
        nav.on_app_thread(self, self.loaded, worlds)
        nav.on_app_thread(self, self.status, done)


def size_label(size) -> str:
    """A byte count as KB under 1 MB, else MB; empty when unknown."""
    if not isinstance(size, (int, float)):
        return ""
    return f"{size / 1024:.0f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"


def size_text(file) -> str:
    """An installed file's size, colored by how big it is: under 1 MB green, under 10 MB yellow, else red."""
    try:
        size = os.path.getsize(file) if file else None
    except OSError:
        return ""
    if size is None:
        return ""
    mb = size / 1024 / 1024
    color = "$success" if mb < 1 else "$warning" if mb < 10 else "$error"
    return f"[{color}]{size_label(size)}[/]"


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
