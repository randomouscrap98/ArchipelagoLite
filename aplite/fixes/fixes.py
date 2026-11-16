"""
Every per-world fix aplite applies: runtime fixes for client code that assumes the kivy GUI exists (FIXES; machinery:
aplite/client_fixes.py) and source patches (PATCHES; machinery: aplite/source_patches.py).

This file also ships between releases: aplite's "Client fixes" menu downloads it from the main branch, and after a
client crash aplite checks it for a fix for that game. So:
- Add a line to LOG for every change (it's the revision: a longer LOG is newer). aplite/fixes/check.sh fails when the
  file changed but LOG didn't grow; run it before pushing a fixes change to main (release.sh and build_dist.sh run it).
- Keep LOG, each FIXES key and each Fix/Patch's first argument and `versions` plain literals: aplite reads them
  without running the file.
- `versions` (the apworld's world_version values a fix was made for) only limits the crash notice: users on a
  version nobody looked at aren't told a fix exists. Fixes and patches apply to every version. Empty: notice for all.
- Only use what aplite.client_fixes / aplite.source_patches provide in this aplite release (older aplites fall back to
  their own copy when this one fails to load).
"""
import asyncio
import sys

from aplite.client_fixes import Fix
from aplite.source_patches import Patch

LOG = [
    "2026-11-05 Warioworld: client couldn't load (imports kivy) and items never arrived (GUI counter calls)",
    "2026-11-05 Luigi's Mansion: error on every connect (GUI labels)",
    "2026-11-05 Donkey Kong Country: no items ever received (GUI barrel label in the game watcher)",
    "2026-11-07 Burnout 3: client exited right after starting without the GUI",
    "2026-11-07 osu!: faster loading (song list dedupe at import)",
    "2026-11-07 Sonic Adventure DX: faster loading (name conversion memoized at import)",
    "2026-11-08 osu!: a failed osu! API request (no network) stopped auto-checks and crashed aplite on exit",
    "2026-11-08 Universal Tracker: /get_logical_path works without the GUI",
    "2026-11-15 Banjo-Tooie: error on connect and on every received item (GUI items tab)",
    "2026-11-15 Banjo-Tooie: patches the ROM and starts the /autostart program on start, like its GUI",
    "2026-11-16 Metroid: Samus Returns: client tab flooded with debug packet logs",
]


def _dkc_barrel_label() -> None:
    from worlds.AutoSNIClient import AutoSNIClientRegister
    handler = AutoSNIClientRegister.game_handlers.get("Donkey Kong Country")
    if handler is not None and hasattr(handler, "handle_barrel_label"):
        async def handle_barrel_label(ctx, ram) -> None:  # its first line imports kvui, so nothing of it can run
            pass

        handler.handle_barrel_label = handle_barrel_label  # on this instance only; the class is untouched


def _bt_patch_and_run(ctx) -> None:
    patch_and_run = getattr(sys.modules[type(ctx).__module__], "patch_and_run", None)
    if patch_and_run is not None:  # kept on ctx: the loop holds tasks only weakly
        ctx._aplite_patch_task = asyncio.create_task(patch_and_run(True), name="Banjo-Tooie patch_and_run")


def _info_only_log(ctx) -> None:
    import logging
    from aplite.tui import log_buffer
    log_buffer.setLevel(logging.INFO)  # aplite 15 does this for every client


FIXES = {
    "Warioworld": Fix("WwClient.py: top-level `import kvui` (client can't load without kivy); "
                      "ctx.ui.spritelingcountupdate unguarded in _give_item / give_items / the item prefill (the "
                      "Dolphin task catches it by disconnecting, so items never arrive)", null_ui=True,
                      stub_imports=("worlds.WarioworldAP.WwClient", ("kvui",))),
    "Luigi's Mansion": Fix("LMClient.py on_package Connected: self.ui.important_labels / update_*_label unguarded "
                           "(error on every connect)", null_ui=True),
    "Donkey Kong Country": Fix("Client.py game_watcher -> handle_barrel_label: `from kvui import` (first line) + "
                               "ctx.ui.connect_layout unguarded, before items are received: no items ever",
                               setup=_dkc_barrel_label),
    "Banjo-Tooie": Fix("BTClient.py: ctx.tab_items is only made in run_gui (GUI build); BanjoTooieItemTracker."
                       "refresh_items sets tab_items.content.data unguarded (error on Connected and every "
                       "ReceivedItems); run_gui is also what starts patch_and_run (patch ROM if missing/outdated, "
                       "then the /autostart program), so it's started here like the GUI does",
                       versions=("4.13.1",), null_attrs=("tab_items",), on_start=_bt_patch_and_run),
    "Metroid: Samus Returns": Fix("client.py launch: Utils.init_logging(loglevel=logging.DEBUG) sets the root logger to "
                                  "DEBUG and connector.py logs every game packet at debug; kvui's log handler is INFO "
                                  "so its GUI hides them, aplite's LogBuffer (before 15) had no level",
                                  versions=("0.3.2",), on_start=_info_only_log),
}
"""Runtime fixes, game -> Fix (aplite/client_fixes.py)."""

PATCHES = [
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
    Patch("tracker/TrackerClient.py", "get_logical_path refuses without the kivy UI (\"No UI, i'm not converting this "
                                      "back to prints\"), though everything after the check prints through "
                                      "ctx.print_json, which aplite's client tab shows; the map highlight it also sets "
                                      "is unused without the UI",
          old="""\
    if not ctx.ui:
        logger.error("No UI, i'm not converting this back to prints, sorry")
        return
""",
          new=""),
    Patch("osu/Client.py", "game_watcher lets a failed osu! API request (no network, DNS) end the task, so auto-checks "
                           "stop and _main's `await progression_watcher` re-raises it on exit, crashing the app; log "
                           "it and retry on the next poll",
          old="""\
            for mode in ctx.auto_modes:
                await get_last_scores(ctx, mode, print)
                await asyncio.sleep(1)
""",
          new="""\
            for mode in ctx.auto_modes:
                try:
                    await get_last_scores(ctx, mode, print)
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                    logger.warning(f"osu! API request failed ({e!r}); retrying")
                await asyncio.sleep(1)
"""),
]
"""Source patches (aplite/source_patches.py): an exact snippet of one apworld module replaced before it's compiled.
Each only applies while its whole `old` snippet appears exactly once, so a world update that touches that code turns
it off by itself. Match the full block whose behavior is replaced; the replacement must do the same thing."""
