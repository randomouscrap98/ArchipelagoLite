"""Machinery for per-world fixes of client code that assumes the kivy GUI exists; the fixes themselves are in
aplite/fixes/fixes.py. aplite keeps ctx.ui None, so `if ctx.ui:` code is skipped, but some worlds touch ctx.ui (or
import kvui) unguarded and crash; they'd crash the same way under upstream's --nogui. Rather than patching their functions,
fixes reshape state: a stand-in for ctx.ui, a no-op for one GUI-only method on a registered handler instance, a
stand-in for a GUI-made context attribute, or a kvui stand-in while one client module is imported. A stand-in
doesn't care which names the world uses, so world updates rarely break a fix.

The fixes file ships with each release and can be updated between releases (aplite/fixes_update.py): a downloaded copy
in the user folder is used instead of the bundled one while its LOG is longer.
To add a fix: one FIXES entry keyed by the world's game name, plus a LOG line. Remove it once the world fixes its
client.
Notes: ignore/claude/research/world-gui-tabs.md (which worlds, where they crash).
Check a fixes change before pushing it: aplite/fixes/check.sh (runs `python -m aplite.client_fixes --check REF...`)."""
from __future__ import annotations

import ast
import asyncio
import dataclasses
import importlib.abc
import logging
import os
import sys
import types
import typing

if typing.TYPE_CHECKING:
    from CommonClient import CommonContext

logger = logging.getLogger("aplite")

BUNDLED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixes", "fixes.py")
"""The fixes file this aplite release ships."""
INSTALLED_NAME = "aplite_client_fixes.py"
"""A downloaded fixes file, in the user folder (Utils.user_path)."""


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
    versions: tuple[str, ...] = ()
    """The apworld's world_version values this fix is for (e.g. "4.13.1"); empty: all. Other versions get nothing."""
    null_ui: bool = False
    """ctx.ui = NullUI() for this game's client contexts."""
    null_attrs: tuple[str, ...] = ()
    """Context attributes the world's GUI would have made (e.g. a tab it fills in), each set to a NullUI."""
    on_start: typing.Callable[[CommonContext], None] | None = None
    """Called once per client context, on the loop tick after it's made: startup work the world only does in run_gui."""
    setup: typing.Callable[[], None] | None = None
    """Run once at startup (worlds loaded) for world-level state, e.g. a registered SNI handler."""
    stub_imports: tuple[str, tuple[str, ...]] | None = None
    """(client module, modules): while that module is imported, each of the modules is a NullUI-backed stand-in (only
    without kivy, i.e. always in aplite). Removed again afterwards, so nothing else sees it."""


FIXES: dict[str, Fix] = {}
"""The fixes in use, from the active fixes file (install())."""


# reading fixes files without running them (downloaded copies are only run once the user installs them)


@dataclasses.dataclass(frozen=True)
class Entry:
    """One FIXES entry, as read from the file's text."""
    why: str
    versions: tuple[str, ...]
    code: str
    """ast.dump of the Fix(...) call: two entries with the same code are the same fix."""


def _assigned(tree: ast.Module, name: str) -> ast.expr | None:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return node.value
    return None


def read_log(source: str) -> list[str]:
    """The file's LOG (its revision is the length). Raises ValueError for a file without a literal LOG list."""
    try:
        value = _assigned(ast.parse(source), "LOG")
        log = ast.literal_eval(value) if value is not None else None
    except (SyntaxError, ValueError, TypeError) as e:
        raise ValueError(f"unreadable fixes file: {e}") from e
    if not isinstance(log, list) or not all(isinstance(line, str) for line in log):
        raise ValueError("fixes file has no LOG list")
    return log


def read_entries(source: str) -> dict[str, Entry]:
    """The file's FIXES entries: game -> Entry. Entries that aren't a Fix(...) call with literal arguments are left
    out."""
    value = _assigned(ast.parse(source), "FIXES")
    entries: dict[str, Entry] = {}
    if not isinstance(value, ast.Dict):
        return entries
    for key, call in zip(value.keys, value.values, strict=True):
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str) and isinstance(call, ast.Call)):
            continue
        keywords = {k.arg: k.value for k in call.keywords if k.arg}
        why_node = call.args[0] if call.args else keywords.get("why")
        try:
            why = ast.literal_eval(why_node) if why_node is not None else ""
            versions = tuple(ast.literal_eval(keywords["versions"])) if "versions" in keywords else ()
        except (ValueError, TypeError):
            continue
        entries[key.value] = Entry(str(why), tuple(str(v) for v in versions), ast.dump(call))
    return entries


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def installed_path() -> str:
    import Utils
    return Utils.user_path(INSTALLED_NAME)


def active_path() -> str:
    """The fixes file to use: a downloaded copy while its LOG is longer than the bundled one's, else the bundled one.
    A downloaded copy that isn't newer (this release already has its fixes) is deleted."""
    installed = installed_path()
    if not os.path.isfile(installed):
        return BUNDLED
    try:
        newer = len(read_log(read_text(installed))) > len(read_log(read_text(BUNDLED)))
    except (OSError, ValueError):
        newer = False
    if newer:
        return installed
    try:
        os.remove(installed)
        logger.info("Removed the downloaded client fixes: this version of aplite already has them.")
    except OSError:
        pass
    return BUNDLED


def load_file(path: str) -> dict[str, Fix]:
    """Run a fixes file and return its FIXES. Raises if it doesn't load here (e.g. it needs a newer aplite)."""
    source = read_text(path)
    read_log(source)
    module = types.ModuleType("aplite_client_fixes_list")
    module.__file__ = path
    exec(compile(source, path, "exec"), module.__dict__)  # not an import: no __pycache__ in the user folder
    fixes = getattr(module, "FIXES", None)
    if not isinstance(fixes, dict) or not all(isinstance(g, str) and isinstance(f, Fix) for g, f in fixes.items()):
        raise ValueError("FIXES isn't a dict of game name -> Fix")
    return fixes


def world_version(game: str) -> str | None:
    """The installed world's world_version as "4.13.1", or None (no such world / no version)."""
    from worlds.AutoWorld import AutoWorldRegister
    world = AutoWorldRegister.world_types.get(game)
    version = getattr(world, "world_version", None)
    return ".".join(str(part) for part in version) if version else None


def applies(versions: tuple[str, ...], game: str) -> bool:
    return not versions or world_version(game) in versions


def game_for_traceback(tb: types.TracebackType | None) -> str | None:
    """The game whose world package the innermost world frame of a traceback is in (e.g. a crash in
    worlds.banjo_tooie.BTClient -> "Banjo-Tooie"), or None."""
    from worlds.AutoWorld import AutoWorldRegister
    packages = {}
    for game, world in AutoWorldRegister.world_types.items():
        parts = world.__module__.split(".")
        if len(parts) >= 2 and parts[0] == "worlds":
            packages.setdefault(".".join(parts[:2]), game)
    found = None
    while tb is not None:
        parts = str(tb.tb_frame.f_globals.get("__name__", "")).split(".")
        found = packages.get(".".join(parts[:2]), found)
        tb = tb.tb_next
    return found


# the fixes in action


def _stub_module(name: str) -> types.ModuleType:
    module = types.ModuleType(name, "aplite stand-in (client_fixes)")
    module.__getattr__ = lambda attr: NullUI()  # PEP 562: `from kvui import GameManager` gets a stand-in
    return module


class _StubbingLoader(importlib.abc.Loader):
    def __init__(self, loader, stubs: tuple[str, ...]) -> None:
        self.loader = loader
        self.stubs = stubs

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


class _StubFinder(importlib.abc.MetaPathFinder):
    """Wraps the loader of the client modules named in FIXES' stub_imports."""

    def __init__(self, targets: dict[str, tuple[str, ...]]) -> None:
        self.targets = targets
        self.busy = False

    def find_spec(self, fullname, path, target=None):
        stubs = self.targets.get(fullname)
        if stubs is None or self.busy:
            return None
        self.busy = True  # the real finders below us find the spec
        try:
            from importlib.util import find_spec
            spec = find_spec(fullname)
        finally:
            self.busy = False
        if spec is None or spec.loader is None:
            return None
        spec.loader = _StubbingLoader(spec.loader, stubs)
        return spec


def _has_ut_map(game: str) -> bool:
    """UT turns its kivy map on when `ctx.ui is not None` and the world defines tracker_world: keep ctx.ui None."""
    from worlds.AutoWorld import AutoWorldRegister
    world = AutoWorldRegister.world_types.get(game)
    return world is not None and hasattr(world, "tracker_world")


def apply(ctx: CommonContext) -> None:
    game = getattr(ctx, "game", None) or ""
    fix = FIXES.get(game)
    if not fix or not applies(fix.versions, game):
        return
    if fix.null_ui and ctx.ui is None and not _has_ut_map(game):
        ctx.ui = NullUI()
    for name in fix.null_attrs:
        if not hasattr(ctx, name):
            setattr(ctx, name, NullUI())
    if fix.on_start and not getattr(ctx, "_aplite_started", False):
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # no loop yet (first apply of a context made outside it): nothing to start on
            return
        ctx._aplite_started = True
        try:
            fix.on_start(ctx)
        except Exception:  # a world's internals changed: the user can still do it by hand (e.g. /patch)
            logger.exception(f"client fix for {game}: startup failed")


def _load_active() -> dict[str, Fix]:
    path = active_path()
    if path != BUNDLED:
        try:
            return load_file(path)
        except Exception:  # e.g. made for a newer aplite: this release's own fixes still work
            logger.warning("The downloaded client fixes don't load in this version of aplite; using its own.",
                           exc_info=True)
    return load_file(BUNDLED)


def install() -> None:
    """Call after the worlds are loaded: loads the fixes, runs the setups, then applies fixes to every client context
    as it's made."""
    from CommonClient import CommonContext
    FIXES.clear()
    FIXES.update(_load_active())
    active = {game: fix for game, fix in FIXES.items() if applies(fix.versions, game)}
    targets = {fix.stub_imports[0]: fix.stub_imports[1] for fix in active.values() if fix.stub_imports}
    if targets:
        sys.meta_path.insert(0, _StubFinder(targets))
    for game, fix in active.items():
        if fix.setup:
            try:
                fix.setup()
            except Exception:  # a world's internals changed: the client may crash as before, aplite must not
                logger.exception(f"client fix for {game} failed")
    original_init = CommonContext.__init__

    def __init__(self, *args, **kwargs) -> None:
        original_init(self, *args, **kwargs)
        apply(self)
        # again once the subclass __init__ is done: some set ctx.game after super().__init__ (Banjo-Tooie).
        # apply is idempotent, and the next tick comes long before any server message
        try:
            asyncio.get_running_loop().call_soon(apply, self)
        except RuntimeError:  # made outside the event loop: only the first apply
            pass

    CommonContext.__init__ = __init__


def _check(refs: list[str]) -> int:
    """aplite/fixes/check.sh: the fixes file must have a longer LOG than at each git ref where its text differs."""
    import subprocess

    from aplite import REPO
    relative = os.path.relpath(BUNDLED, REPO).replace(os.sep, "/")
    current = read_text(BUNDLED)
    try:
        length = len(read_log(current))
        read_entries(current)
    except (ValueError, SyntaxError) as e:
        print(f"{relative}: {e}", file=sys.stderr)
        return 1
    failed = False
    for ref in refs:
        shown = subprocess.run(["git", "-C", REPO, "show", f"{ref}:{relative}"], capture_output=True, text=True)
        if shown.returncode != 0:  # no such ref, or the file isn't there yet
            print(f"{relative}: not in {ref}, skipped")
            continue
        if shown.stdout == current:
            continue
        try:
            before = len(read_log(shown.stdout))
        except ValueError:
            before = -1
        if length <= before:
            print(f"{relative}: changed since {ref} but LOG wasn't updated (add a line for this change)",
                  file=sys.stderr)
            failed = True
        else:
            print(f"{relative}: {length - before} new LOG line(s) since {ref}")
    return 1 if failed else 0


if __name__ == "__main__":
    if sys.argv[1:2] != ["--check"]:
        sys.exit("usage: python -m aplite.client_fixes --check REF...")
    sys.exit(_check(sys.argv[2:]))
