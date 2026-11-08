"""
Source patches for apworld modules: an exact snippet in one module replaced before it's compiled (bytecode_cache's
zipimport hook, so zipped .apworld files only). For world code that's slow or broken without kivy where no runtime fix
fits, e.g. work done while the module is imported.

A patch applies only while its whole `old` snippet appears exactly once, so a world update that touches that code
(a fix, a rewrite) turns it off by itself, and unrelated updates keep it. Match the full block whose behavior is being
replaced, not one line. The replacement must do the same thing. Remove an entry once the world fixes its code.
"""
from __future__ import annotations

import dataclasses
import logging

logger = logging.getLogger("aplite")


@dataclasses.dataclass(frozen=True)
class Patch:
    module: str
    """Path inside the apworld, e.g. osu/Items.py."""
    why: str
    """The world bug (and its cost), so the entry can be dropped once fixed."""
    old: str
    new: str


PATCHES: list[Patch] = [
    Patch("osu/Items.py", "get_song_data dedupes beatmapsets with `not in list` (O(n^2) dict compares, ~0.6s of every "
                          "start, at import); by id instead, the key find_beatmapset uses",
          old="""\
    beatmapsets = []
    for pack in packs:
        for beatmapset in pack["beatmapsets"]:
            if beatmapset not in beatmapsets:
                beatmapsets.append(beatmapset)
""",
          new="""\
    beatmapsets = []
    _aplite_seen = set()
    for pack in packs:
        for beatmapset in pack["beatmapsets"]:
            if beatmapset["id"] not in _aplite_seen:
                _aplite_seen.add(beatmapset["id"])
                beatmapsets.append(beatmapset)
"""),
    Patch("sadx/Enums.py", "pascal_to_space runs 12 regexes per call and Locations.get_location_name_by_level rebuilds "
                           "every capsule/enemy name 77 times at import (232k calls, ~1.2s here, ~2.5s on Windows); "
                           "memoized (pure function of a str)",
          old="""\
def pascal_to_space(s):
    s = re.sub(r'^(Ss)', 'S.S.', s)
""",
          new="""\
@__import__("functools").cache
def pascal_to_space(s):
    s = re.sub(r'^(Ss)', 'S.S.', s)
"""),
    Patch("burnout3/B3Client.py", "async_main without the GUI awaits ctx.server_task, which ends at once with no server "
                                  "address (\"Please connect...\", then the client exits); wait for quit as "
                                  "CommonClient does (aplite's TUI fallback starts the console)",
          old="    if gui_enabled:\n"
              "        ctx.run_gui()\n"
              "        logger.info(\"GUI Launched\")\n"
              "        await ctx.ui_task \n"  # sic: trailing space
              "    else:\n"
              "        await ctx.server_task\n",
          new="    if gui_enabled:\n"
              "        ctx.run_gui()\n"
              "        logger.info(\"GUI Launched\")\n"
              "        await ctx.ui_task \n"
              "    else:\n"
              "        await ctx.exit_event.wait()\n"),
]


def apply(pathname: str, source: bytes) -> bytes:
    """`source` of the module at `pathname` (zipimport's: ...apworld/osu/Items.py), patched where a patch matches."""
    path = pathname.replace("\\", "/")
    for patch in PATCHES:
        if not path.endswith("/" + patch.module):
            continue
        text = source.decode("utf-8").replace("\r\n", "\n")  # compile() normalizes line endings anyway
        if text.count(patch.old) != 1:
            logger.debug(f"source patch for {patch.module} doesn't match (world changed?); not applied")
            continue
        source = text.replace(patch.old, patch.new).encode("utf-8")
        logger.debug(f"source patch applied to {pathname}")
    return source
