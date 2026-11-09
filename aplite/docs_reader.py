"""Documentation: reads docs/aplite/*.md in the terminal (shipped in every build), starting at its README. Links to
other .md files open here; web links open in the browser."""
from __future__ import annotations

import re
from pathlib import Path

import Utils
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Markdown, MarkdownViewer, OptionList
from textual.widgets.option_list import Option

from aplite.desktop import open_link
from aplite.nav import AppliteApp

LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)\)")
"""[text](target) in the Markdown source; not images."""


def home() -> Path:
    return Path(Utils.local_path("docs", "aplite", "README.md"))


def is_web(href: str) -> bool:
    return bool(re.match(r"[a-z][a-z0-9+.-]*:", href, re.I))  # http:, https:, mailto: ...


class Viewer(MarkdownViewer):
    async def _on_markdown_link_clicked(self, message: Markdown.LinkClicked) -> None:
        message.stop()
        message.prevent_default()  # MarkdownViewer's own handler would treat a web link as a file
        await self.screen.follow(message.href)


class LinksModal(ModalScreen[str | None]):
    """The current page's links, for the keyboard (Markdown links only take clicks)."""
    CSS = """
    LinksModal { align: center middle; }
    LinksModal Vertical { width: 80; max-width: 90%; height: auto; max-height: 80%; border: thick $primary;
                          background: $surface; }
    LinksModal OptionList { height: auto; border: none; }
    """
    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def __init__(self, links: list[tuple[str, str]]) -> None:
        super().__init__()
        self.links = links

    def compose(self) -> ComposeResult:
        with Vertical():
            yield OptionList(*[Option(Text.assemble(text, ("  " + href, "dim")), id=str(i))
                               for i, (text, href) in enumerate(self.links)])

    def on_mount(self) -> None:
        self.query_one(Vertical).border_title = "Links"
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(self.links[int(event.option.id)][1])


class DocsScreen(Screen[None]):
    BINDINGS = [Binding("ctrl+o", "links", "Links"), Binding("backspace", "back", "Back"),
                Binding("ctrl+t", "toggle_contents", "Contents"), Binding("escape", "close", "Close")]

    def compose(self) -> ComposeResult:
        yield Viewer(show_table_of_contents=False, open_links=False)
        yield Footer()

    async def on_mount(self) -> None:
        viewer = self.query_one(Viewer)
        viewer.focus()
        if not home().is_file():
            await viewer.document.update(f"# Documentation\n\nThe documentation isn't installed (looked for "
                                         f"`{home()}`).")
            return
        await viewer.go(home())

    def page(self) -> Path:
        return Markdown.sanitize_location(str(self.query_one(Viewer).navigator.location))[0]

    async def follow(self, href: str) -> None:
        if is_web(href):
            open_link(self, href)
            return
        target, _ = Markdown.sanitize_location(href)
        if str(target) != "." and not (self.page().parent / target).is_file():
            self.notify(f"Page not found: {href}", severity="error")
            return
        await self.query_one(Viewer).go(href)

    def action_links(self) -> None:
        try:
            source = self.page().read_text(encoding="utf-8")
        except OSError:
            source = ""
        links = LINK.findall(source)
        if not links:
            self.notify("No links on this page.")
            return

        async def picked(href: str | None) -> None:
            if href:
                await self.follow(href)
        self.app.push_screen(LinksModal(links), picked)

    async def action_back(self) -> None:
        await self.query_one(Viewer).back()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        return action != "close" or self.app.screen_stack[0] is not self  # on top of the launcher, not alone

    def action_close(self) -> None:
        self.dismiss()

    def action_toggle_contents(self) -> None:
        viewer = self.query_one(Viewer)
        viewer.show_table_of_contents = not viewer.show_table_of_contents


class DocsApp(AppliteApp):
    TITLE = "Documentation"

    def get_default_screen(self) -> Screen:
        return DocsScreen()


def run(*args: str) -> None:
    DocsApp().run()


def component():
    """Launcher entry for this tool (added to the component list by aplite)."""
    from worlds.LauncherComponents import Component, Type
    return Component("Documentation", func=run, component_type=Type.TOOL,
                     description="How to set up and use Archipelago Lite, readable right here.")
