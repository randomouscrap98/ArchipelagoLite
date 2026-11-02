"""
Local copy of the website's options pages (player-options and weighted-options), using upstream's own routes,
templates and JS from WebHostLib, served on 127.0.0.1.

WebHostLib/__init__.py pulls in pony, caching, compression etc., so it is never run: a stand-in WebHostLib package
(whose __path__ is the real folder) provides only what WebHostLib/options.py imports. Needs flask.
"""
from __future__ import annotations

import os
import sys
import types
import typing

import Utils

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
    from flask import Flask, redirect, request

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

    @app.errorhandler(404)
    def elsewhere(error):
        # site links in the page headers (other pages of archipelago.gg) go to the real site
        return redirect(f"https://archipelago.gg{request.full_path.rstrip('?')}")

    return app
