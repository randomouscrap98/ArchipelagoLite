"""aplite preferences, stored in aplite.yaml next to host.yaml. Keys: theme, glyphs, colors (colors.py), webhost_url,
background_spares (background.py), and the folder each file picker was last in (its pref_key, "..._dir")."""
from __future__ import annotations

import logging
import os
import typing

import yaml

import Utils
from aplite import OFFICIAL_SITE, write_atomic

if typing.TYPE_CHECKING:
    from textual.app import App

DEFAULTS: dict[str, typing.Any] = {
    "theme": "textual-dark",
    "webhost_url": OFFICIAL_SITE,  # Host on Archipelago uploads here (a self-hosted WebHost works too)
    "background_spares": 0,
}

_prefs: dict[str, typing.Any] | None = None
"""What aplite.yaml holds (no defaults: saving must not freeze them into the file)."""


def path() -> str:
    return Utils.user_path("aplite.yaml")


def _load() -> dict[str, typing.Any]:
    global _prefs
    if _prefs is None:
        _prefs = {}
        if os.path.exists(path()):
            try:
                with open(path(), encoding="utf-8") as f:
                    _prefs.update(yaml.safe_load(f) or {})
            except Exception as e:
                logging.warning(f"Could not read {path()}, using defaults: {e}")
    return _prefs


def reset() -> None:
    """Forget the loaded prefs: the next get() reads aplite.yaml again (another aplite process may have changed it)."""
    global _prefs
    _prefs = None


def get(key: str) -> typing.Any:
    return _load().get(key, DEFAULTS.get(key))


def put(key: str, value: typing.Any) -> None:
    """Save one pref. Re-reads aplite.yaml first and changes only `key`: other aplite processes (windows, background
    spares) may have saved theirs since this one loaded, and writing this process's old copy would undo them."""
    global _prefs
    if get(key) == value:
        return
    _prefs = None
    prefs = _load()
    prefs[key] = value
    try:
        write_atomic(path(), yaml.safe_dump(prefs, sort_keys=True))
    except OSError as e:
        logging.warning(f"Could not save {path()}: {e}")


def bind_theme(app: "App") -> None:
    """Apply the saved theme to a Textual app and save it whenever the user changes it."""
    theme = get("theme")
    if theme in app.available_themes:
        app.theme = theme
    app.theme_changed_signal.subscribe(app, lambda theme: put("theme", theme.name))
