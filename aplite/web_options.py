"""
Local copy of the website's options pages (player-options and weighted-options), using upstream's own routes,
templates and JS from WebHostLib, served on 127.0.0.1.

WebHostLib/__init__.py pulls in pony, caching, compression etc., so it is never run: a stand-in WebHostLib package
(whose __path__ is the real folder) provides only what WebHostLib/options.py imports. Needs flask.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import types
import typing

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, RichLog, Static

import Utils
from aplite import site_pages
from aplite.desktop import open_path
from aplite.nav import AppliteApp

if typing.TYPE_CHECKING:
    from flask import Flask

WEBHOST = Utils.local_path("WebHostLib")


class _NoCache:
    """Stand-in for flask_caching's Cache: options.py only uses @cache.cached()."""

    @staticmethod
    def cached(*args, **kwargs):
        return lambda func: func


def get_world_theme(game_name: str) -> str:
    """Same result as WebHostLib.misc.get_world_theme, without importing misc (which needs pony)."""
    from worlds.AutoWorld import AutoWorldRegister
    world = AutoWorldRegister.world_types.get(game_name)
    theme = world.web.theme if world else "grass"
    return theme if os.path.exists(os.path.join(WEBHOST, "templates", "header", f"{theme}Header.html")) else "grass"


def create_app() -> Flask:
    from flask import Flask, redirect

    app = Flask("WebHostLib", root_path=WEBHOST)  # templates/ and static/ from the real WebHostLib
    app.config["MAX_FORM_MEMORY_SIZE"] = 2 * 1024 * 1024  # as upstream: large pages such as SC2
    app.jinja_env.filters["any"] = any
    app.jinja_env.filters["all"] = all
    app.jinja_env.filters["get_file_safe_name"] = Utils.get_file_safe_name
    app.jinja_env.filters["title_sorted"] = Utils.title_sorted

    package = types.ModuleType("WebHostLib")
    package.__path__ = [WEBHOST]
    package.app = app
    package.cache = _NoCache()
    generate = types.ModuleType("WebHostLib.generate")
    generate.get_meta = lambda *args, **kwargs: {}
    generate.start_generation = lambda *args, **kwargs: (  # the page's "Generate Game" button
        "Generating a game isn't available in this local copy. Use Export Options to save the YAML, "
        "then generate with Generate (or on archipelago.gg).", 501)
    misc = types.ModuleType("WebHostLib.misc")
    misc.get_world_theme = get_world_theme
    for name, module in (("WebHostLib", package), ("WebHostLib.generate", generate), ("WebHostLib.misc", misc)):
        if name in sys.modules and sys.modules[name] is not module:
            raise RuntimeError(f"{name} is already imported; the stand-in must come first")
        sys.modules[name] = module

    from WebHostLib import options  # registers upstream's routes and template filters on our app
    package.options = options

    @app.route("/")
    def index():
        return redirect("/games")

    @app.route("/games")
    def games():
        from flask import render_template_string
        from worlds.AutoWorld import AutoWorldRegister
        from aplite.options_creator import has_options_page
        worlds = {game: world for game, world in sorted(AutoWorldRegister.world_types.items(),
                                                        key=lambda item: item[0].lower()) if has_options_page(world)}
        return render_template_string(GAMES_PAGE, worlds=worlds)

    @app.errorhandler(404)
    def elsewhere(error):
        return site_pages.to_official_site()  # site links in the page headers (other pages of the site)

    return app


GAMES_PAGE = site_pages.page("Options (local)", """
        <h1>Options for installed games</h1>
        <p>A local copy of archipelago.gg's options pages, served by aplite for the worlds installed here
           (including custom apworlds). Export Options saves a YAML for Generate.</p>
        <table>
            {% for game, world in worlds.items() %}
            <tr>
                <td>{{ game }}</td>
                {% if world.web.options_page is string %}
                <td colspan="2"><a href="{{ world.web.options_page }}">Options on the game's website</a></td>
                {% else %}
                <td><a href="{{ url_for('player_options', game=game) }}">Player options</a></td>
                <td><a href="{{ url_for('weighted_options', game=game) }}">Weighted options</a></td>
                {% endif %}
            </tr>
            {% endfor %}
        </table>
""")


class WebOptionsApp(AppliteApp):
    TITLE = "Web Options"
    CSS = """
    #link { padding: 1 2; height: auto; }
    RichLog { height: 1fr; border-top: solid $panel; }
    RichLog:focus { border-top: solid $border; }
    """
    BINDINGS = [Binding("ctrl+o", "open_browser", "Open browser")]

    def __init__(self) -> None:
        super().__init__()
        self.server = None
        self.url = ""

    def compose(self) -> ComposeResult:
        yield Static("Starting server…", id="link")
        yield RichLog(wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        from werkzeug.serving import make_server
        self.server = make_server("127.0.0.1", 0, create_app(), threaded=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}/games"
        handler = _LogToApp(self)
        logging.getLogger("werkzeug").addHandler(handler)
        logging.getLogger("werkzeug").propagate = False  # requests show here, not in the log file per request
        threading.Thread(target=self.server.serve_forever, name="web options", daemon=True).start()
        self.query_one("#link", Static).update(Text.assemble(
            "Options pages are running at  ", (self.url, "bold underline"), "\n",
            ("Opened in your browser; if it didn't open, copy the link. Leaving (Ctrl+Q / Ctrl+L) stops the server.",
             "dim")))
        open_path(self.url)  # no webbrowser fallback: it may pick a terminal browser, drawn over this app

    def log_line(self, line: str) -> None:
        self.query_one(RichLog).write(line)

    def action_open_browser(self) -> None:
        if not open_path(self.url):
            self.notify(f"No browser opener found; open {self.url}", timeout=10)

    def on_unmount(self) -> None:
        if self.server:
            self.server.shutdown()


class _LogToApp(logging.Handler):
    def __init__(self, app: WebOptionsApp) -> None:
        super().__init__()
        self.app = app

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.app.call_from_thread(self.app.log_line, record.getMessage())
        except Exception:
            pass  # app shutting down


def run(*args: str) -> None:  # a component func: the launcher may pass args, which it doesn't use
    WebOptionsApp().run()


def component():
    """Launcher entry for this tool (added to the component list by aplite)."""
    from worlds.LauncherComponents import Component, Type
    return Component("Web Options", func=run, component_type=Type.TOOL,
                     description="The website's options pages for your installed games, served locally in your "
                                 "browser.")
