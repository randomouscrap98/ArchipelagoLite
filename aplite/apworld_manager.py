"""Install/Update Apworlds: a Textual face for the APWorld Manager apworld (silasary). Its own UIs are kivy (off in
aplite) and curses (needs utils.curses_utils, newer than our base), so without this it prints a notice and exits.
Uses the world's own API, as its curses.py does: repositories, refresh_apworld_table, install_world. Research:
ignore/claude/research/apworld-manager.md."""
from __future__ import annotations

import contextlib
import os
import re
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
from aplite.master_detail import TextScroll
from aplite.combo import FilterInput, highlight_first, keep_scroll
from aplite.desktop import open_link

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


@contextlib.contextmanager
def worldless_apworlds(manager) -> typing.Iterator[None]:
    """The manager lists installed apworlds by walking AutoWorldRegister.world_types, so ones that register no World
    (client-only, like bizhawk_ut) were missing from the installed list and kept being offered for install. While it
    builds the table, give each such file in the user folder a stand-in entry; it only reads zip_path, __doc__ and
    world_version there (the version comes from the file's hash or manifest)."""
    import pathlib
    from worlds import AutoWorldRegister, user_folder
    if not user_folder or not os.path.isdir(user_folder):
        yield
        return
    registered = {pathlib.Path(world.zip_path).resolve() for world in AutoWorldRegister.world_types.values() if world.zip_path}
    extras = {}
    for file in pathlib.Path(user_folder).glob("*.apworld"):
        if file.resolve() in registered or not file.is_file():
            continue
        title = next((r.name for r in remotes(manager, file.stem).values() if r.name), None)
        extras[title or file.stem] = type(file.stem, (), {"zip_path": file, "__doc__": "", "__module__": __name__})
    if not extras:
        yield
        return
    original = AutoWorldRegister.world_types
    AutoWorldRegister.world_types = {**extras, **original}
    try:
        yield
    finally:
        AutoWorldRegister.world_types = original


def remotes(manager, world_id: str) -> dict:
    """The versions the enabled repositories list for a world: {version: remote}."""
    return manager.repositories.packages_by_id_version.get(world_id) or {}


def newest(manager, versions: typing.Iterable) -> typing.Any:
    """The newest of some remotes, by full version (pre/post releases included)."""
    return max(versions, key=lambda r: manager.parse_version(r.world_version))


def manifest_repo(manifest: dict) -> str | None:
    """The repository an apworld's manifest names."""
    return manifest.get("repo_url") or manifest.get("github")


def repo_url(world: dict) -> str | None:
    """Where a world lives: the manifest's repo_url/github, else the repo of a GitHub release download (nearly every
    index entry), else the folder the download comes from. The index has no repo field, and the apworld's own
    release_url is built from the index's address for index worlds (not a github.com link, so None)."""
    if url := manifest_repo(world.get("manifest") or {}):
        return url
    remote = world.get("latest_version") or next(iter((world.get("remotes") or {}).values()), None)
    download = remote.data.get("world") if remote else None
    if not download:
        return None
    return release_repo(download) or download.rsplit("/", 1)[0]


RELEASE_DOWNLOAD = re.compile(r"(https?://[^/]+/[^/]+/[^/]+)/releases/download/([^/]+)/")


def release_repo(download: str | None) -> str | None:
    """The repo of a GitHub release download URL (https://github.com/owner/repo), else None."""
    match = RELEASE_DOWNLOAD.match(download or "")
    return match.group(1) if match else None


def release_page(download: str | None) -> str | None:
    """The release page (notes, changelog) of a GitHub release download URL, else None."""
    match = RELEASE_DOWNLOAD.match(download or "")
    return f"{match.group(1)}/releases/tag/{match.group(2)}" if match else None


def short_repo(url: str) -> str:
    return url.split("://", 1)[-1].split("/", 1)[-1]  # owner/repo


def repo_change(version, latest) -> str | None:
    """Where `version` comes from when its repo differs from `latest`'s (ApWorldMetadata): "another maintainer"
    (another owner), "an old repo" (same owner, another repo), or None (same, or not GitHub releases). Older
    versions sometimes come from a different fork or maintainer (Hexcells Infinite <= 1.2)."""
    mine, theirs = release_repo(version.data.get("world")), release_repo(latest.data.get("world"))
    if not mine or not theirs or mine.lower() == theirs.lower():
        return None
    return "another maintainer" if mine.lower().split("/")[-2] != theirs.lower().split("/")[-2] else "an old repo"


class VersionPicker(ModalScreen):
    """Every listed version of one world, newest first, with the highlighted one explained in a box below. Enter
    installs; an older one than installed needs a second Enter (nav.Confirm). Dismisses with the chosen
    ApWorldMetadata, or None."""
    CSS = """
    VersionPicker { align: center middle; }
    VersionPicker Vertical { width: 80%; max-width: 110; height: auto; max-height: 80%; border: thick $primary;
                             background: $surface; }
    VersionPicker #picker-title { padding: 0 1; }
    VersionPicker OptionList { height: auto; border: none; }  /* max-height: fit_list */
    VersionPicker #picker-doc { height: 4; border-top: solid $panel; padding: 0 1; }
    VersionPicker #picker-doc:focus { border-top: solid $border; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+o", "open_release", "Open release page")]

    def __init__(self, title: str, versions: list, installed, parse) -> None:
        """versions: ApWorldMetadata newest first; installed: the installed version (parsed) or None."""
        super().__init__()
        self.title_text = title
        self.versions = versions
        self.installed = installed
        self.parse = parse
        self.newest_by_hash: dict[str, str] = {}
        """File hash -> the newest version with it: an author re-uploading a newer file to an older release leaves the
        older version pointing at the newer file (Balatro's v0.1.9e now serves post3's file)."""
        for version in versions:
            if digest := version.data.get("hash_sha256"):
                self.newest_by_hash.setdefault(digest, version.world_version)
        self.confirm_downgrade = nav.Confirm()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"[b]{self.title_text}[/b]: pick a version", id="picker-title")
            width = max(len(v.world_version) for v in self.versions)
            yield OptionList(*[Option(self.label(v, width)) for v in self.versions])
            yield TextScroll(Static(id="picker-info"), id="picker-doc")

    def on_mount(self) -> None:
        self.fit_list()
        self.explain(0)

    def on_resize(self) -> None:
        self.fit_list()

    def fit_list(self) -> None:
        """Cap the list so the info box stays inside the modal (80% of the screen, less border, title and box)."""
        self.query_one(OptionList).styles.max_height = max(3, min(30, int(self.size.height * 0.8) - 2 - 1 - 4))

    def replaced_by(self, version) -> str | None:
        """The newer version whose file this one now serves (see newest_by_hash), if any."""
        newest = self.newest_by_hash.get(version.data.get("hash_sha256"), version.world_version)
        return newest if newest != version.world_version else None

    def prerelease(self, version) -> bool:
        return version.is_prerelease or self.parse(version.world_version).is_prerelease  # flagged, or a1/b2/rc0/dev

    def label(self, version, width: int) -> str:
        date = (version.created_at or "")[:10]
        parts = [f"{version.world_version:<{width}}", f"{date:<10}", f"{size_label(version.data.get('size')):>8}"]
        if newest := self.replaced_by(version):
            parts.append(f"[$error]replaced with {newest}[/]")
        if self.prerelease(version):
            parts.append("[$warning]prerelease[/]")
        if self.installed is not None and self.parse(version.world_version) == self.installed:
            parts.append("[$success]installed[/]")
        if repo_change(version, self.versions[0]):
            parts.append("[$warning]\\[!][/]")
        return "  ".join(parts)

    def explain(self, index: int | None) -> None:
        """Fill the box below the list for the version at `index`: release page, then what its tags mean."""
        lines = []
        if index is not None:
            version = self.versions[index]
            download = version.data.get("world")
            if page := release_page(download):
                lines.append(f'(Ctrl+O): [link="{page}"]{page.removeprefix("https://")}[/link]')
            elif download:
                lines.append(f"Download: {download}")
            if change := repo_change(version, self.versions[0]):
                lines.append(f"[$warning]\\[!] from {change}:[/] {short_repo(release_repo(download))} (the newest version "
                             f"is from {short_repo(release_repo(self.versions[0].data.get('world')))})")
            if self.installed is not None and self.parse(version.world_version) < self.installed:
                lines.append("Older than installed: Enter twice to downgrade.")
            if newest := self.replaced_by(version):
                lines.append(f"Replaced with {newest}: author changed release, you will not get "
                             f"{version.world_version}.")
            if self.prerelease(version):
                lines.append("Prerelease: may be unfinished or unstable.")
        self.query_one("#picker-info", Static).update("\n".join(lines))
        self.query_one("#picker-doc", TextScroll).scroll_home(animate=False)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        self.explain(event.option_index)

    def action_open_release(self) -> None:
        index = self.query_one(OptionList).highlighted
        page = release_page(self.versions[index].data.get("world")) if index is not None else None
        if not page:
            self.notify("No release page for this version.")
        else:
            open_link(self, page)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        index = event.option_index
        version = self.versions[index]
        older = self.installed is not None and self.parse(version.world_version) < self.installed
        if older and not self.confirm_downgrade(self, index, f"{version.world_version} is older than the installed "
                                                                  f"version: a downgrade. Press Enter again to install it."):
            return
        self.dismiss(version)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ManagerApp(nav.AppliteApp):
    TITLE = "APWorld Manager"
    BINDINGS = [Binding("ctrl+r", "reload", "Refresh"), Binding("ctrl+t", "delete", "Delete"),
                Binding("ctrl+y", "versions", "Versions"), Binding("ctrl+o", "open_repo", "Open repo")]
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
        self.confirm_delete = nav.Confirm()

    def world_table(self) -> list[dict]:
        """refresh_apworld_table, with the latest version of uninstalled worlds picked by full version: it compares only
        major.minor.micro there, so ties (0.1.9a0 vs 0.1.9.post3) went to whichever the index lists last (Balatro
        installed a 2024 release). Installed worlds already compare full versions."""
        with worldless_apworlds(self.manager):
            worlds = self.manager.refresh_apworld_table()
        for world in worlds:
            listed = remotes(self.manager, world["id"])
            if not world["installed"] and world.get("latest_version") and listed:
                world["latest_version"] = newest(self.manager, listed.values())
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
            yield Static(id="repo-note")
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
        self.app.call_from_thread(self.status, "Fetching repositories...")
        try:
            repos = self.manager.repositories
            repos.load_repos_from_settings()
            repos.refresh()
            worlds = self.world_table()
        except Exception as e:  # network, a repo with bad data, or a changed world_manager API
            self.app.call_from_thread(self.status, f"Couldn't load the world list: {e}")
            return
        finally:
            self.busy = False
        self.app.call_from_thread(self.loaded, worlds)

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
        heading = size = description = link = note = ""
        if world:
            latest = world.get("latest_version")
            version = f"  (latest {latest.world_version})" if latest else ""
            if world["installed"]:
                version = f"  installed {world['installed_version']}{version}"
                size = size_text(world.get("file"))
                if note := self.installed_repo_note(world):
                    version += "  [$warning]\\[!][/]"
            heading = f"[b]{world['title']}[/b]{version}"
            description = world["description"]
            if url := repo_url(world):
                link = f'[link="{url}"]{url.removeprefix("https://")}[/link]  (Ctrl+O opens)'
        self.query_one("#heading", Static).update(heading)
        self.query_one("#link", Static).update(link)
        self.query_one("#link", Static).display = bool(link)
        self.query_one("#repo-note", Static).update(note)
        self.query_one("#repo-note", Static).display = bool(note)
        self.query_one("#size", Static).update(size)
        self.query_one("#description", Static).update(description)

    def installed_repo_note(self, world: dict) -> str:
        """A warning line when the installed version comes from another repo than the newest listed one."""
        listed = remotes(self.manager, world["id"])
        if not listed or not world.get("installed_version"):
            return ""
        parse = self.manager.parse_version
        latest = newest(self.manager, listed.values())
        installed = next((r for r in listed.values()
                          if parse(r.world_version) == parse(world["installed_version"])), None)
        change = installed and repo_change(installed, latest)
        if not change:
            return ""
        return (f"[$warning]\\[!] installed version is from {change}: {short_repo(release_repo(installed.data['world']))}"
                f"[/] (the newest is from {short_repo(release_repo(latest.data['world']))}). Ctrl+Y lists versions.")

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
        if not self.confirm_delete(self, file, f"Press Ctrl+T again to delete {os.path.basename(file)}."):
            return
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
        else:
            open_link(self, url)

    def action_versions(self) -> None:
        world = self.highlighted()
        if world is None or self.busy:
            return
        parse = self.manager.parse_version
        listed = remotes(self.manager, world["id"])
        if not listed:
            self.status(f"No versions listed for {world['title']}.")
            return
        versions = sorted(listed.values(), key=lambda r: parse(r.world_version), reverse=True)
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
                url = manifest_repo(manifest)
                self.app.call_from_thread(self.status, f"Adding repository {url}...")
                RepoWorld.settings.repositories[url] = True
                RepoWorld.settings._changed = True
                Utils.get_settings().save()  # aplite returns to the launcher by exec: no atexit autosave
                self.manager.repositories.add_repo(url)
                self.manager.repositories.refresh()
                done = f"Added repository {url}."
            else:
                name = f"{world['title']} {version}" if version else world["title"]
                self.app.call_from_thread(self.status, f"Installing {name}...")
                with captured_messageboxes(messages):
                    self.manager.install_world(world)
                errors = [text for text, error in messages if error]
                if errors:
                    self.app.call_from_thread(self.status, f"Install failed: {errors[0]}")
                    return
                self.installed_any = True
                done = f"Installed {name}. New worlds load next time the launcher starts."
            worlds = self.world_table()
        except Exception as e:
            self.app.call_from_thread(self.status, f"Install failed: {e}")
            return
        finally:
            self.busy = False
        self.app.call_from_thread(self.loaded, worlds)
        self.app.call_from_thread(self.status, done)


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
