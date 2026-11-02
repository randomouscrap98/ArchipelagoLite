"""
Run an Archipelago component in the terminal.

Import order matters: patches must land before CommonClient/clients import the patched names.
Worlds are loaded by load(), which the launcher runs in the background so it can show immediately.
"""
import os
import sys
import tempfile

import ModuleUpdate

import aplite
from aplite.requirements import lite_requirements


def _use_lite_requirements() -> None:
    """Swap core requirements.txt for a copy without kivy, so ModuleUpdate doesn't try to install it."""
    lines = lite_requirements(ModuleUpdate.core_constraints)
    lite = os.path.join(tempfile.gettempdir(), "aplite-requirements.txt")
    with open(lite, "w") as f:
        f.writelines(lines)
    ModuleUpdate.requirements_files.discard(ModuleUpdate.core_constraints)
    ModuleUpdate.requirements_files.add(lite)


_use_lite_requirements()
ModuleUpdate.update()

import Utils

Utils.gui_enabled = False  # before CommonClient does `from Utils import gui_enabled`

from aplite import bytecode_cache, components, launcher, prompts, tui

bytecode_cache.install()  # before any .apworld is imported
prompts.install()  # terminal file prompts; before worlds load (LauncherComponents imports open_filename by name)

_loaded = False


def load() -> list:
    """Load all worlds and apply the patches that need them. Returns the launcher components. Safe to call twice
    (the launcher runs it in a thread; direct launch calls it up front)."""
    global _loaded
    if not _loaded:
        import CommonClient  # loads all worlds
        CommonClient.console_loop = tui.console_loop  # before clients do `from CommonClient import console_loop`
        tui.install_fallback()
        import worlds.LauncherComponents as LauncherComponents
        LauncherComponents.run_in_process = True  # core hook: clients need this terminal's stdin
        LauncherComponents.components.extend(components.extra_components())
        _loaded = True
    import worlds.LauncherComponents as LauncherComponents
    return LauncherComponents.components


def main() -> None:
    # everything after the target goes to the component untouched, so `--` isn't needed
    argv = sys.argv[1:]
    if argv[:1] in (["-h"], ["--help"]):
        print(__doc__.strip().splitlines()[0])
        print("usage: python -m aplite [Patch|Game|Component|url] [component args]")
        print("With no arguments, opens the launcher.")
        return
    target = argv[0] if argv else None
    component_args = argv[1:]
    if component_args[:1] == ["--"]:
        component_args = component_args[1:]

    Utils.init_logging("aplite")
    tui.log_buffer.attach()  # so a client's TUI can replay startup messages

    if target is None:
        launch_from_launcher()
        return

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
            components.run(component, target, *component_args)
        return

    from worlds.LauncherComponents import identify
    file, component = identify(target)
    if not component:
        sys.exit(f"Unknown component, patch or game: {target}")  # Launcher.main would open the kivy GUI
    components.run(component, *((file,) if file else ()), *component_args)


def launch_from_launcher() -> None:
    choice = launcher.pick(load)
    if not choice:
        return
    component, args, come_back = choice
    try:
        components.run(component, *args)
    except SystemExit:
        if not come_back:
            raise
    if not come_back:
        return
    if components.pauses_after(component):
        try:
            input("\nPress Enter to return to the launcher…")
        except EOFError:
            pass
    # a fresh process rather than looping, so nothing the component left behind leaks into the launcher
    sys.stdout.flush()
    os.execve(sys.executable, [sys.executable, "-m", "aplite"], aplite.child_env())


if __name__ == "__main__":
    main()
