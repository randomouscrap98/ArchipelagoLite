"""Client color roles (item kinds, players, locations, hint statuses), editable in the Theme Explorer.

Upstream's JSONtoTextParser colors server messages with 8 ANSI names, which Textual maps through one fixed palette per
dark/light mode (in its dark one, red and magenta are the same). Instead, the client's parser (RoleParser) tags each
role with a marker color, and RoleFilter (a Textual line filter, so it runs at draw time: switching themes recolors
old lines too) swaps the marker for the role's color: the user's dark/light choice in normal themes, upstream's ANSI
name in ANSI themes (the terminal's palette). Saved in aplite.yaml as colors: {dark: {role: "#RRGGBB"}, light: {...}},
overrides only."""
from __future__ import annotations

import re
import typing

from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from textual.filter import LineFilter

from aplite import prefs

if typing.TYPE_CHECKING:
    from textual.app import App


class Role(typing.NamedTuple):
    label: str
    used_for: str
    ansi: str
    """Upstream's color, as a standard ANSI name: used in ANSI themes."""
    dark: str
    light: str


# defaults = what Textual showed for upstream's ANSI colors (Monokai dark, Alabaster light); light "white" is
# Alabaster's background, so unspecified hints get its foreground instead
ROLES: dict[str, Role] = {
    "item_progression": Role("Progression item", "items considered by logic that unlock progress", "magenta",
                             "#F4005F", "#7A3E9D"),
    "item_useful": Role("Useful item", "helpful but not required items", "blue", "#9D65FF", "#325CC0"),
    "item_trap": Role("Trap item", "harms a player", "red", "#F4005F", "#AA3731"),
    "item_filler": Role("Filler item", "everything else", "cyan", "#58D1EB", "#0083B2"),
    "location": Role("Location", "a check", "green", "#98E024", "#448C27"),
    "entrance": Role("Entrance", "entrance names in hints", "blue", "#9D65FF", "#325CC0"),
    "player_self": Role("Your name", "you (and groups you're in)", "magenta", "#F4005F", "#7A3E9D"),
    "player_other": Role("Other player", "everyone else", "yellow", "#FD971F", "#CB9000"),
    "hint_found": Role("Hint: found", "hint status", "green", "#98E024", "#448C27"),
    "hint_unspecified": Role("Hint: unspecified", "hint status", "white", "#C4C5B5", "#000000"),
    "hint_no_priority": Role("Hint: no priority", "hint status", "blue", "#9D65FF", "#325CC0"),
    "hint_avoid": Role("Hint: avoid", "hint status", "red", "#F4005F", "#AA3731"),
    "hint_priority": Role("Hint: priority", "hint status", "magenta", "#F4005F", "#7A3E9D"),
}
MODES = ("dark", "light")
HEX = re.compile(r"#?([0-9a-fA-F]{6})")

MARKER = (0x01, 0xA9)
"""Red and green of the marker colors (blue = role index): a truecolor nothing else is likely to print."""
_INDEX = {name: index for index, name in enumerate(ROLES)}
_BY_INDEX = list(ROLES)


def marker(role: str) -> Color:
    return Color.from_rgb(*MARKER, _INDEX[role])


def escape(role: str) -> str:
    """ANSI escape that starts the role's marker color (Text.from_ansi reads it back)."""
    return "\033[38;2;%d;%d;%dm" % (*MARKER, _INDEX[role])


def style(role: str) -> Style:
    return Style(color=marker(role))


def normalize(value: str) -> str | None:
    """'#rrggbb' / 'rrggbb' -> '#RRGGBB'; None if it isn't one."""
    match = HEX.fullmatch(value.strip())
    return f"#{match.group(1).upper()}" if match else None


def overrides() -> dict[str, dict[str, str]]:
    saved = prefs.get("colors") or {}
    return {mode: dict(saved.get(mode) or {}) for mode in MODES}


def color(role: str, mode: str) -> str:
    saved = overrides()[mode].get(role)
    return normalize(saved) if saved and normalize(saved) else getattr(ROLES[role], mode)


def default(role: str, mode: str) -> str:
    return getattr(ROLES[role], mode)


def is_custom(role: str, mode: str) -> bool:
    return color(role, mode) != default(role, mode)


def save(role: str, values: dict[str, str | None]) -> None:
    """Set a role's color per mode (None or the default: drop the override)."""
    saved = overrides()
    for mode, value in values.items():
        if value is None or value == default(role, mode):
            saved[mode].pop(role, None)
        else:
            saved[mode][role] = value
    prefs.put("colors", {mode: roles for mode, roles in saved.items() if roles})


def reset_all() -> None:
    prefs.put("colors", {})


class RoleFilter(LineFilter):
    """Swaps role markers for the role's color for the app's current theme (see module docstring)."""

    def __init__(self, app: App) -> None:
        super().__init__()
        self.app = app
        self._cache: dict[tuple[Style, str], Style] = {}

    def mode(self) -> str:
        if self.app.native_ansi_color:
            return "ansi"
        return "dark" if self.app.current_theme.dark else "light"

    def clear(self) -> None:
        self._cache.clear()

    def _swap(self, style: Style, mode: str) -> Style:
        key = (style, mode)
        if key not in self._cache:
            role = _BY_INDEX[style.color.triplet.blue]
            new = Color.parse(ROLES[role].ansi if mode == "ansi" else color(role, mode))
            self._cache[key] = style + Style(color=new)
        return self._cache[key]

    def apply(self, segments: list[Segment], background) -> list[Segment]:
        mode = None
        result = segments
        for index, (text, style, control) in enumerate(segments):
            triplet = style and style.color and style.color.triplet
            if triplet and (triplet.red, triplet.green) == MARKER and triplet.blue < len(_BY_INDEX):
                if mode is None:
                    mode = self.mode()
                    result = list(segments)
                result[index] = Segment(text, self._swap(style, mode), control)
        return result


def install(app: App) -> None:
    """Put a RoleFilter first in the app's line filters (private Textual list; ANSIToTruecolor comes after)."""
    if not any(isinstance(f, RoleFilter) for f in app._filters):
        app._filters.insert(0, RoleFilter(app))


def refresh(app: App) -> None:
    """Redraw everything after a color change (the theme didn't change, so Textual won't)."""
    for f in app._filters:
        if isinstance(f, RoleFilter):
            f.clear()
    for screen in app.screen_stack:
        for widget in [screen, *screen.walk_children()]:
            widget._styles_cache.clear()
            widget.refresh()


def make_parser(ctx):
    """The client's JSONtoTextParser with each role tagged by its marker color. Built here, not at import: NetUtils
    costs ~150ms and the launcher imports nav."""
    from NetUtils import JSONtoTextParser, HintStatus, color_code

    hint_roles = {HintStatus.HINT_FOUND: "hint_found", HintStatus.HINT_UNSPECIFIED: "hint_unspecified",
                  HintStatus.HINT_NO_PRIORITY: "hint_no_priority", HintStatus.HINT_AVOID: "hint_avoid",
                  HintStatus.HINT_PRIORITY: "hint_priority"}

    class RoleParser(JSONtoTextParser):
        def _handle_color(self, node):
            role = node.get("aplite_role")
            if role is None:  # color nodes from the server ("red;bold" etc.)
                return super()._handle_color(node)
            return escape(role) + self._handle_text(node) + color_code("reset")

        def _handle_player_id(self, node):
            self_ = self.ctx.slot_concerns_self(int(node["text"]))
            node["aplite_role"] = "player_self" if self_ else "player_other"
            return super()._handle_player_id(node)

        def _handle_player_name(self, node):
            node["aplite_role"] = "player_other"
            return super()._handle_player_name(node)

        def _handle_item_name(self, node):
            flags = node.get("flags", 0)  # same order as upstream: progression wins
            node["aplite_role"] = ("item_progression" if flags & 0b001 else "item_useful" if flags & 0b010 else
                                   "item_trap" if flags & 0b100 else "item_filler")
            return super()._handle_item_name(node)

        def _handle_location_name(self, node):
            node["aplite_role"] = "location"
            return super()._handle_location_name(node)

        def _handle_entrance_name(self, node):
            node["aplite_role"] = "entrance"
            return super()._handle_entrance_name(node)

        def _handle_hint_status(self, node):
            node["aplite_role"] = hint_roles.get(node["hint_status"])
            return super()._handle_hint_status(node)

    return RoleParser(ctx)
