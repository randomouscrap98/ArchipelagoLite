"""
Every client fix aplite applies: per-world workarounds for client code that assumes the kivy GUI exists (machinery and
how fixes work: aplite/client_fixes.py).

This file also ships between releases: aplite's "Client fixes" menu downloads it from the main branch, and after a
client crash aplite checks it for a fix for that game. So:
- Add a line to LOG for every change (it's the revision: a longer LOG is newer). aplite/fixes/check.sh fails when the
  file changed but LOG didn't grow; run it before pushing a fixes change to main (release.sh and build_dist.sh run it).
- Keep LOG and each FIXES key, `why` and `versions` plain literals: aplite reads them without running the file.
- Give a fix `versions` (the apworld's world_version values it was made for) when it targets a custom apworld, so it
  isn't applied to a later, rewritten client. Empty: every version.
- Only use what aplite.client_fixes provides in this aplite release (older aplites fall back to their own copy when
  this one fails to load).
"""
import asyncio
import sys

from aplite.client_fixes import Fix

LOG = [
    "2026-11-05 Warioworld, Luigi's Mansion, Donkey Kong Country: GUI-only code crashed the clients",
    "2026-11-15 Banjo-Tooie 4.13.1: tab_items crash on connect; patch ROM and autostart on start like the GUI",
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
}
