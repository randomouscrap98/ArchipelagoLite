"""aplite preferences, stored in aplite.yaml next to host.yaml."""
from __future__ import annotations

import logging
import os
import typing

import yaml

import Utils

if typing.TYPE_CHECKING:
    from textual.app import App

DEFAULTS: dict[str, typing.Any] = {
    "theme": "textual-dark",
}

_prefs: dict[str, typing.Any] | None = None


def path() -> str:
    return Utils.user_path("aplite.yaml")


def _load() -> dict[str, typing.Any]:
    global _prefs
    if _prefs is None:
        _prefs = dict(DEFAULTS)
        if os.path.exists(path()):
            try:
                with open(path(), encoding="utf-8") as f:
                    _prefs.update(yaml.safe_load(f) or {})
            except Exception as e:
                logging.warning(f"Could not read {path()}, using defaults: {e}")
    return _prefs


def get(key: str) -> typing.Any:
    return _load().get(key, DEFAULTS.get(key))


def set(key: str, value: typing.Any) -> None:
    prefs = _load()
    if prefs.get(key) == value:
        return
    prefs[key] = value
    try:
        with open(path(), "w", encoding="utf-8") as f:
            yaml.safe_dump(prefs, f, sort_keys=True)
    except OSError as e:
        logging.warning(f"Could not save {path()}: {e}")


def bind_theme(app: "App") -> None:
    """Apply the saved theme to a Textual app and save it whenever the user changes it."""
    theme = get("theme")
    if theme in app.available_themes:
        app.theme = theme
    app.theme_changed_signal.subscribe(app, lambda theme: set("theme", theme.name))
