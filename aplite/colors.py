"""Client color roles (item kinds, players, locations, hint statuses), editable in Client Colors (client_colors.py).

Upstream's JSONtoTextParser colors server messages with 8 ANSI names, which Textual maps through one fixed palette per
dark/light mode (in its dark one, red and magenta are the same). Instead, the client's parser (RoleParser) tags each
role with a marker color, and RoleFilter (a Textual line filter, so it runs at draw time: switching themes recolors
old lines too) swaps the marker for the role's color: the user's dark/light choice in normal themes, upstream's ANSI
name in ANSI themes (the terminal's palette). Saved in aplite.yaml as colors: {dark: {role: "#RRGGBB"}, light: {...}},
overrides only."""
from __future__ import annotations

import functools
import re
import typing

from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from textual.filter import LineFilter

from aplite import glyphs, prefs

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
    "tracker_in_logic": Role("Tracker: in logic", "Universal Tracker locations (also unconnected)", "green",
                             "#98E024", "#448C27"),
    "tracker_out_of_logic": Role("Tracker: out of logic", "Universal Tracker locations (also its errors)", "red",
                                 "#F4005F", "#AA3731"),
    "tracker_glitched": Role("Tracker: glitched", "Universal Tracker locations", "yellow", "#E6DB74", "#CB9000"),
    "tracker_hinted": Role("Tracker: hinted", "Universal Tracker locations", "blue", "#6A9EFF", "#325CC0"),
    "tracker_hinted_in_logic": Role("Tracker: hinted, in logic", "Universal Tracker locations", "cyan",
                                    "#58D1EB", "#0083B2"),
    "tracker_hinted_glitched": Role("Tracker: hinted, glitched", "Universal Tracker locations", "bright_yellow",
                                    "#FD971F", "#B85C00"),
    "echo": Role("Your commands", "what you typed, echoed in the log", "yellow", "#FD971F", "#B85C00"),
    "connected": Role("Connected", "client status line (and self-host: Running)", "green", "#98E024", "#448C27"),
    "not_connected": Role("Not connected", "client status line", "red", "#F4005F", "#AA3731"),
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


def default(role: str, mode: str) -> str:
    return getattr(ROLES[role], mode)


def color(role: str, mode: str) -> str:
    saved = normalize(overrides()[mode].get(role) or "")
    return saved or default(role, mode)


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


def theme_mode(app: App) -> str:
    """Which role colors the app's theme uses: "dark" or "light" (ANSI themes: see RoleFilter.mode)."""
    return "dark" if app.current_theme.dark else "light"


def hint_roles() -> dict[int, str]:
    """Color role per hint status. Built on use: importing NetUtils takes ~150ms, which the launcher shouldn't pay."""
    from NetUtils import HintStatus
    return {HintStatus.HINT_FOUND: "hint_found", HintStatus.HINT_UNSPECIFIED: "hint_unspecified",
            HintStatus.HINT_NO_PRIORITY: "hint_no_priority", HintStatus.HINT_AVOID: "hint_avoid",
            HintStatus.HINT_PRIORITY: "hint_priority"}


class RoleFilter(LineFilter):
    """Swaps role markers for the role's color for the app's current theme (see module docstring)."""

    def __init__(self, app: App) -> None:
        super().__init__()
        self.app = app
        self._cache: dict[tuple[Style, str], Style] = {}

    def mode(self) -> str:
        return "ansi" if self.app.native_ansi_color else theme_mode(self.app)

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
        dim = self.app.current_theme.variables.get("aplite-dim")
        if dim:
            if result is segments:
                result = list(segments)
            for index, (text, style, control) in enumerate(result):
                if style and style.dim:
                    result[index] = Segment(text, _undim(style, dim), control)
        return result


def is_light(red: int, green: int, blue: int) -> bool:
    """Whether a background is light enough for black text (perceived brightness)."""
    return 0.299 * red + 0.587 * green + 0.114 * blue > 140


@functools.lru_cache(maxsize=1024)
def _undim(style: Style, color: str) -> Style:
    """Dim text in a theme with aplite-dim (nav.HIGH_CONTRAST; terminals fade dim): that color on dark backgrounds,
    the text's own color on light ones (cursor rows: gray on cyan / yellow is unreadable)."""
    background = style.bgcolor and style.bgcolor.get_truecolor()
    if background and is_light(*background):
        return style + Style(dim=False)
    return style + Style(dim=False, color=color)


def install(app: App) -> None:
    """Put a RoleFilter first in the app's line filters (private Textual list; ANSIToTruecolor comes after)."""
    if not any(isinstance(f, RoleFilter) for f in app._filters):
        app._filters.insert(0, RoleFilter(app))


def refresh(app: App) -> None:
    """Redraw everything after a color change (the theme didn't change, so Textual won't)."""
    for f in app._filters:
        if isinstance(f, RoleFilter):
            f.clear()
    glyphs.redraw_all(app)


def make_parser(ctx):
    """The client's JSONtoTextParser with each role tagged by its marker color. Built here, not at import: NetUtils
    costs ~150ms and the launcher imports nav."""
    from NetUtils import JSONtoTextParser, color_code
    roles_by_status = hint_roles()

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
            node["aplite_role"] = roles_by_status.get(node["hint_status"])
            return super()._handle_hint_status(node)

    return RoleParser(ctx)
