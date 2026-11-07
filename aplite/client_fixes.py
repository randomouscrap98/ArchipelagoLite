"""Per-world fixes for client code that assumes the kivy GUI exists. aplite keeps ctx.ui None, so `if ctx.ui:` code is
skipped, but some worlds touch ctx.ui (or import kvui) unguarded and crash; they'd crash the same way under upstream's
--nogui. Rather than patching their functions, these reshape state: a stand-in for ctx.ui, a no-op for one GUI-only method
on a registered handler instance, or a kvui stand-in while one client module is imported. A stand-in doesn't care which names the world uses, so world
updates rarely break a fix.

To add a fix: one FIXES entry keyed by the world's game name. Remove it once the world fixes its client.
Notes: ignore/claude/research/world-gui-tabs.md (which worlds, where they crash)."""
from __future__ import annotations

import asyncio
import dataclasses
import importlib.abc
import logging
import sys
import types
import typing

if typing.TYPE_CHECKING:
    from CommonClient import CommonContext

logger = logging.getLogger("aplite")


class NullUI:
    """Absorbs everything: attributes, calls, [...], assignments. False, so `if ctx.ui:` code stays skipped (the GUI
    paths of CommonClient and the world)."""

    def __bool__(self) -> bool:
        return False

    def __getattr__(self, name: str) -> NullUI:
        if name.startswith("__"):  # copy/pickle/inspect probes: behave like a plain object
            raise AttributeError(name)
        return self

    def __call__(self, *args, **kwargs) -> NullUI:
        return self

    def __getitem__(self, key) -> NullUI:
        return self

    def __setattr__(self, name: str, value) -> None:
        pass

    def __setitem__(self, key, value) -> None:
        pass

    def __iter__(self) -> typing.Iterator:
        return iter(())

    def __repr__(self) -> str:
        return "NullUI()"


@dataclasses.dataclass(frozen=True)
class Fix:
    why: str
    """The world bug this works around (file:line in the apworld), so it can be dropped once fixed."""
    null_ui: bool = False
    """ctx.ui = NullUI() for this game's client contexts."""
    setup: typing.Callable[[], None] | None = None
    """Run once at startup (worlds loaded) for world-level state, e.g. a registered SNI handler."""
    stub_imports: tuple[str, tuple[str, ...]] | None = None
    """(client module, modules): while that module is imported, each of the modules is a NullUI-backed stand-in (only
    without kivy, i.e. always in aplite). Removed again afterwards, so nothing else sees it."""
    after_import: tuple[str, typing.Callable[[types.ModuleType], None]] | None = None
    """(client module, hook): the hook gets that module right after it's imported, e.g. to swap a GUI-only method."""


def _dkc_barrel_label() -> None:
    from worlds.AutoSNIClient import AutoSNIClientRegister
    handler = AutoSNIClientRegister.game_handlers.get("Donkey Kong Country")
    if handler is not None and hasattr(handler, "handle_barrel_label"):
        async def handle_barrel_label(ctx, ram) -> None:  # its first line imports kvui, so nothing of it can run
            pass

        handler.handle_barrel_label = handle_barrel_label  # on this instance only; the class is untouched


def _b3_tui(module: types.ModuleType) -> None:
    from aplite import tui

    def run_gui(self) -> None:  # instead of its kivy GameManager: aplite's TUI, which ends when the user quits
        self.input_task = self.ui_task = asyncio.create_task(tui.console_loop(self), name="Input")

    module.gui_enabled = True  # its GUI path awaits ui_task; the other one returns once server_task does
    module.PS2Context.run_gui = run_gui


FIXES: dict[str, Fix] = {
    "Warioworld": Fix("WwClient.py: top-level `import kvui` (client can't load without kivy); "
                      "ctx.ui.spritelingcountupdate unguarded in _give_item / give_items / the item prefill (the Dolphin "
                      "task catches it by disconnecting, so items never arrive)", null_ui=True,
                      stub_imports=("worlds.WarioworldAP.WwClient", ("kvui",))),
    "Luigi's Mansion": Fix("LMClient.py on_package Connected: self.ui.important_labels / update_*_label unguarded "
                           "(error on every connect)", null_ui=True),
    "Donkey Kong Country": Fix("Client.py game_watcher -> handle_barrel_label: `from kvui import` (first line) + "
                               "ctx.ui.connect_layout unguarded, before items are received: no items ever",
                               setup=_dkc_barrel_label),
    "Burnout 3": Fix("B3Client.py async_main: without the GUI it awaits ctx.server_task, which ends at once with no "
                     "server address (\"Please connect...\" then exit)",
                     after_import=("worlds.burnout3.B3Client", _b3_tui)),
}


def _stub_module(name: str) -> types.ModuleType:
    module = types.ModuleType(name, "aplite stand-in (client_fixes)")
    module.__getattr__ = lambda attr: NullUI()  # PEP 562: `from kvui import GameManager` gets a stand-in
    return module


class _StubbingLoader(importlib.abc.Loader):
    def __init__(self, loader, stubs: tuple[str, ...], after: typing.Callable | None = None) -> None:
        self.loader = loader
        self.stubs = stubs
        self.after = after

    def create_module(self, spec):
        return self.loader.create_module(spec)

    def exec_module(self, module) -> None:
        import importlib.util
        if importlib.util.find_spec("kivy") is not None:  # a real GUI stack: leave the imports alone
            added = []
        else:
            added = [name for name in self.stubs if name not in sys.modules]
        for name in added:
            sys.modules[name] = _stub_module(name)
        try:
            self.loader.exec_module(module)
        finally:
            for name in added:
                sys.modules.pop(name, None)
        if self.after:
            try:
                self.after(module)
            except Exception:  # the world's client changed: it may misbehave as before, aplite must not
                logger.exception(f"client fix for {module.__name__} failed")


class _StubFinder(importlib.abc.MetaPathFinder):
    """Wraps the loader of the client modules named in FIXES' stub_imports and after_import."""

    def __init__(self, targets: dict[str, tuple[tuple[str, ...], typing.Callable | None]]) -> None:
        self.targets = targets
        self.busy = False

    def find_spec(self, fullname, path, target=None):
        entry = self.targets.get(fullname)
        if entry is None or self.busy:
            return None
        self.busy = True  # the real finders below us find the spec
        try:
            from importlib.util import find_spec
            spec = find_spec(fullname)
        finally:
            self.busy = False
        if spec is None or spec.loader is None:
            return None
        spec.loader = _StubbingLoader(spec.loader, *entry)
        return spec


def _has_ut_map(game: str) -> bool:
    """UT turns its kivy map on when `ctx.ui is not None` and the world defines tracker_world: keep ctx.ui None."""
    from worlds.AutoWorld import AutoWorldRegister
    world = AutoWorldRegister.world_types.get(game)
    return world is not None and hasattr(world, "tracker_world")


def apply(ctx: CommonContext) -> None:
    fix = FIXES.get(getattr(ctx, "game", None) or "")
    if fix and fix.null_ui and ctx.ui is None and not _has_ut_map(ctx.game):
        ctx.ui = NullUI()


def install() -> None:
    """Call after the worlds are loaded: runs the setups, then applies fixes to every client context as it's made."""
    from CommonClient import CommonContext
    targets = {fix.stub_imports[0]: (fix.stub_imports[1], None) for fix in FIXES.values() if fix.stub_imports}
    targets.update({fix.after_import[0]: ((), fix.after_import[1]) for fix in FIXES.values() if fix.after_import})
    if targets:
        sys.meta_path.insert(0, _StubFinder(targets))
    for game, fix in FIXES.items():
        if fix.setup:
            try:
                fix.setup()
            except Exception:  # a world's internals changed: the client may crash as before, aplite must not
                logger.exception(f"client fix for {game} failed")
    original_init = CommonContext.__init__

    def __init__(self, *args, **kwargs) -> None:
        original_init(self, *args, **kwargs)
        apply(self)

    CommonContext.__init__ = __init__
