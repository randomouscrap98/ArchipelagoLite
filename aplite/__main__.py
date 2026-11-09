"""
Run an Archipelago component in the terminal.

Import order matters: patches must land before CommonClient/clients import the patched names.
Worlds are loaded by load(), which the launcher runs in the background so it can show immediately.
"""
import atexit
import logging
import os
import signal
import sys
import tempfile
import threading
import time

if hasattr(signal, "SIGHUP"):  # an inherited "ignore" (some launchers) would keep us alive after the terminal closes
    signal.signal(signal.SIGHUP, signal.SIG_DFL)

from aplite import background

if __name__ == "__main__":  # before anything heavy: a background spare may run this open instead
    background.try_handoff(sys.argv[1:])

import ModuleUpdate

import aplite
from aplite.requirements import lite_requirements


def _use_lite_requirements() -> None:
    """Swap core requirements.txt for a copy without kivy, so ModuleUpdate doesn't try to install it."""
    if ModuleUpdate.update_ran:  # SKIP_REQUIREMENTS_UPDATE (aplite.sh, dist launchers): no check, nothing to swap
        return
    lines = lite_requirements(ModuleUpdate.core_constraints)
    # per user: /tmp is shared, and another user's copy can't be overwritten
    fd, lite = tempfile.mkstemp(prefix="aplite-requirements-", suffix=".txt")
    with os.fdopen(fd, "w") as f:
        f.writelines(lines)
    atexit.register(_remove, lite)
    ModuleUpdate.requirements_files.discard(ModuleUpdate.core_constraints)
    ModuleUpdate.requirements_files.add(lite)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


_use_lite_requirements()
ModuleUpdate.update()

import Utils

Utils.gui_enabled = False  # before CommonClient does `from Utils import gui_enabled`

from aplite import bytecode_cache, client_fixes, components, launcher, nav, prompts, startup_log, tui

bytecode_cache.install()  # before any .apworld is imported
prompts.install()  # terminal file prompts; before worlds load (LauncherComponents imports open_filename by name)

_loaded = False
_load_lock = threading.Lock()


def load() -> list:
    """Load all worlds and apply the patches that need them. Returns the launcher components. Safe to call twice, also
    at once (the launcher runs it in a thread; direct launch calls it up front; a background spare starts it early)."""
    global _loaded
    with _load_lock:
        _load()
    import worlds.LauncherComponents as LauncherComponents
    return LauncherComponents.components


def _load() -> None:
    global _loaded
    if not _loaded:
        level = logging.getLogger().level
        started = time.perf_counter()
        import CommonClient  # loads all worlds
        loaded = time.perf_counter() - started
        # NetUtils' first `import _speedups` without a compiled one has pyximport build it, and setuptools sets the
        # root logger to WARNING: every INFO line (chat, prompts, /help) would vanish. Upstream works around it too.
        logging.getLogger().setLevel(level)
        CommonClient.console_loop = tui.console_loop  # before clients do `from CommonClient import console_loop`
        tui.install_fallback()
        client_fixes.install()
        import worlds.LauncherComponents as LauncherComponents
        LauncherComponents.run_in_process = True  # core hook: clients need this terminal's stdin
        LauncherComponents.components.extend(components.extra_components())
        components.rename_old_components(LauncherComponents.components)
        _loaded = True
        startup_log.report(loaded)
        bytecode_cache.flush()  # now, not at exit: a relaunch (execve) skips atexit
        start_background()


def start_background() -> None:
    """Keep in background is on but its manager isn't running (first open after a reboot): start it now that this
    open's own load is done."""
    if not background.SUPPORTED or background.in_spare or os.environ.get("APLITE_NO_BACKGROUND"):
        return
    from aplite import prefs
    count = prefs.get(background.PREF) or 0
    if count > 0 and not background.running():
        background.start_manager(count, background_log())


def background_log() -> str:
    return Utils.user_path("logs", "aplite_background.txt")


def fingerprint() -> list:
    """What a background spare's loaded state depends on (it's replaced when this changes)."""
    return background.tree_fingerprint(
        [Utils.user_path("custom_worlds"), Utils.local_path("worlds"), os.path.dirname(os.path.abspath(aplite.__file__))],
        [Utils.user_path("host.yaml"), *background.install_files()])


def become_spare() -> None:
    """`python -m aplite --spare` (started by the background manager): load worlds, wait for an open to take over,
    then pick up what changed since this process started."""
    Utils.init_logging("aplite")
    tui.log_buffer.attach()
    background.spare_start_load(load)
    background.serve_spare(fingerprint)
    from aplite import glyphs, prefs
    prefs.reset()  # aplite.yaml may have changed meanwhile
    glyphs.apply(glyphs.chosen_mode())  # depends on the opener's environment (WT_SESSION, TERM)


def main() -> None:
    # everything after the target goes to the component untouched, so `--` isn't needed
    argv = sys.argv[1:]
    spare = argv[:1] == [background.SPARE_ARG]
    if spare:
        become_spare()
        argv = sys.argv[1:]  # the opener's
    if argv[:1] in (["-h"], ["--help"]):
        print(__doc__.strip().splitlines()[0])
        print("usage: python -m aplite [patch file|component|archipelago:// url] [component args]")
        print("With no arguments, opens the launcher.")
        print("  --list      list the components and the patch files they open")
        print("  --version   print the version")
        print("  <component> --help   the component's own options (clients: --connect, --name, ...)")
        return
    if argv[:1] == ["--list"]:
        list_components()
        return
    if argv[:1] == ["--version"]:
        from aplite.version import version
        print(f"Archipelago Lite {version()}")
        return
    target = argv[0] if argv else None
    component_args = argv[1:]
    if component_args[:1] == ["--"]:
        component_args = component_args[1:]

    if not spare:
        Utils.init_logging("aplite")
        tui.log_buffer.attach()  # so a client's TUI can replay startup messages

    if target is None:
        launch_from_launcher()
        return

    from aplite import datadir
    if datadir.migration:  # direct launch: no launcher to show it in
        print(datadir.message(datadir.migration))
        aplite.wait_for_enter("Press Enter to continue…")
        datadir.migration = None

    load()
    if target.startswith("archipelago://"):
        import Launcher
        clients, text_client = Launcher.handle_uri(target)
        if not clients:
            component = text_client
        elif len(clients) == 1:
            component = clients[0]
        else:
            choice = launcher.pick([*clients, text_client], target)
            component = choice[0] if choice else None
        if component:
            run(component, (target, *component_args), come_back=False)
        return

    from worlds.LauncherComponents import identify
    file, component = identify(components.RENAMED.get(target, target))
    if not component:
        sys.exit(f"Unknown component, patch or game: {target}")  # Launcher.main would open the kivy GUI
    run(component, (*((file,) if file else ()), *component_args), come_back=False)


def list_components() -> None:
    """--list: runnable components by type, with the names `python -m aplite <name>` accepts."""
    import shutil
    import textwrap
    from worlds.LauncherComponents import SuffixIdentifier, Type
    rows = []
    for c in load():
        if c.type == Type.HIDDEN or not components.runnable(c):
            continue
        names = c.display_name + (f" ({c.script_name})" if c.script_name and c.script_name != c.display_name else "")
        suffixes = c.file_identifier.suffixes if isinstance(c.file_identifier, SuffixIdentifier) else ()
        rows.append((c.type.name.lower(), names, " ".join(suffixes)))
    width = max(len(r[1]) for r in rows)
    columns = max(shutil.get_terminal_size().columns, width + 30)
    for kind in dict.fromkeys(sorted(r[0] for r in rows)):
        print(f"{kind}:")
        for _, names, suffixes in sorted(r for r in rows if r[0] == kind):
            lines = textwrap.wrap(suffixes, columns - width - 4) or [""]
            print(f"  {names:<{width}}  {lines[0]}".rstrip())
            for line in lines[1:]:
                print(" " * (width + 4) + line)
    print('\nRun one with: python -m aplite "<name or patch file>" [args]; add --help for its options.')


def launch_from_launcher() -> None:
    choice = launcher.pick(load)
    if not choice:
        return
    component, args, come_back = choice
    run(component, args, come_back)


def run(component, args: tuple[str, ...], come_back: bool) -> None:
    """Run a component; then the launcher if the user asked for it (ctrl+l) or `come_back` and they didn't quit."""
    def wants_launcher() -> bool:
        return nav.next_step == "launcher" or (come_back and nav.next_step != "quit")

    try:
        components.run(component, *args)
    except SystemExit as e:
        if nav.quit_requested(e.code):  # ctrl+q in a file prompt before the component had a TUI
            sys.exit(0)
        if not wants_launcher():
            raise
    except Exception:
        if not wants_launcher():
            raise
        report_crash(component)
        crashed = True
    else:
        crashed = False
    if not wants_launcher():
        return
    if crashed or (nav.next_step is None and components.pauses_after(component)):
        aplite.wait_for_enter()
    # a fresh process rather than looping, so nothing the component left behind leaks into the launcher
    bytecode_cache.flush()  # what the component imported (execve skips atexit)
    if background.in_spare:
        sys.stdout.flush()
        sys.exit(aplite.RELAUNCH)  # the thin client that opened us asks for the next spare
    background.relaunch()


def report_crash(component) -> None:
    """A component raised: log it, show the traceback, and say the launcher comes back (a fresh process, so the
    crash's leftovers don't carry over)."""
    import traceback
    logging.getLogger("aplite").exception(f"{component.display_name} crashed", extra={"NoStream": True})
    log_files = [h.baseFilename for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]
    sys.stdout.flush()
    print(file=sys.stderr)
    traceback.print_exc()
    sys.stderr.flush()
    print(f"\n{component.display_name} crashed (traceback above"
          f"{', also in ' + log_files[0] if log_files else ''}). Returning to the launcher is safe.")


if __name__ == "__main__":
    main()
