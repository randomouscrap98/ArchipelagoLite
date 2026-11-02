"""
Run an Archipelago component in the terminal.

Import order matters: patches must land before CommonClient/clients import the patched names.
"""
import logging
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ModuleUpdate


def _use_lite_requirements() -> None:
    """Swap core requirements.txt for a copy without kivy, so ModuleUpdate doesn't try to install it."""
    with open(ModuleUpdate.core_constraints) as f:
        lines = [line for line in f if not re.match(r"\s*kivy", line, re.IGNORECASE)]
    lite = os.path.join(tempfile.gettempdir(), "aplite-requirements.txt")
    with open(lite, "w") as f:
        f.writelines(lines)
    ModuleUpdate.requirements_files.discard(ModuleUpdate.core_constraints)
    ModuleUpdate.requirements_files.add(lite)


_use_lite_requirements()
ModuleUpdate.update()

import Utils

Utils.gui_enabled = False  # before CommonClient does `from Utils import gui_enabled`

import CommonClient  # loads all worlds
from aplite import tui

CommonClient.console_loop = tui.console_loop  # before clients do `from CommonClient import console_loop`
tui.install_fallback()

import worlds.LauncherComponents as LauncherComponents

LauncherComponents.run_in_process = True  # core hook: clients need this terminal's stdin

import Launcher


def main() -> None:
    # everything after the target goes to the component untouched, so `--` isn't needed
    argv = sys.argv[1:]
    if argv[:1] in (["-h"], ["--help"]):
        print(__doc__.strip().splitlines()[0])
        print("usage: python -m aplite [Patch|Game|Component|url] [component args]")
        print("With no arguments, lists components.")
        return
    component_args = argv[1:]
    if component_args[:1] == ["--"]:
        component_args = component_args[1:]
    args = {"Patch|Game|Component|url": argv[0] if argv else None, "args": tuple(component_args),
            "update_settings": False}

    target = args["Patch|Game|Component|url"]
    if target is None:
        print("Components:")
        for component in LauncherComponents.components:
            print(f"  {component.display_name}")
        return

    Utils.init_logging("aplite")
    logging.getLogger().addHandler(tui.log_buffer)  # so the TUI can replay startup messages

    if target.startswith("archipelago://"):
        # Launcher would show a kivy popup to pick a client; until the TUI launcher exists, use the first match
        clients, text_client = Launcher.handle_uri(target)
        Launcher.run_component((clients or [text_client])[0], target, *args["args"])
        return

    _, component = LauncherComponents.identify(target)
    if not component:
        sys.exit(f"Unknown component, patch or game: {target}")  # Launcher.main would open the kivy GUI
    # some clients parse sys.argv themselves and/or take no func args (e.g. civ6, kh1)
    sys.argv = [sys.argv[0], *component_args]
    if component.func and not _accepts_args(component.func):
        args["args"] = ()
    Launcher.main(args)


def _accepts_args(func) -> bool:
    import inspect
    try:
        params = inspect.signature(func).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL) for p in params)


if __name__ == "__main__":
    main()
