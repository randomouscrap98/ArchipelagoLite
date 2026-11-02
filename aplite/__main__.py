"""
Run an Archipelago component in the terminal.

Import order matters: patches must land before CommonClient/clients import the patched names.
Worlds are loaded by load(), which the launcher runs in the background so it can show immediately.
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

from aplite import bytecode_cache, launcher, prompts, tui

bytecode_cache.install()  # before any .apworld is imported
prompts.install()  # terminal file prompts; before worlds load (LauncherComponents imports open_filename by name)


def load() -> list:
    """Load all worlds and apply the patches that need them. Returns the launcher components. Safe to call twice."""
    global LauncherComponents, Launcher
    if "Launcher" in globals():
        return LauncherComponents.components
    import CommonClient  # loads all worlds

    CommonClient.console_loop = tui.console_loop  # before clients do `from CommonClient import console_loop`
    tui.install_fallback()

    import worlds.LauncherComponents as LauncherComponents
    LauncherComponents.run_in_process = True  # core hook: clients need this terminal's stdin
    from aplite import web_options
    LauncherComponents.components.append(web_options.component())  # aplite's own tools

    import Launcher
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
    logging.getLogger().addHandler(tui.log_buffer)  # so the TUI can replay startup messages

    if target is None:
        choice = launcher.pick(load)
        if not choice:
            return
        component, args, come_back = choice
        try:
            run(component, *args)
        except SystemExit:
            if not come_back:
                raise
        if come_back and launcher.pauses_after(component):
            try:
                input("\nPress Enter to return to the launcher…")
            except EOFError:
                pass
        if come_back:
            # a fresh process rather than looping, so nothing the client left behind leaks into the launcher
            sys.stdout.flush()
            os.execv(sys.executable, [sys.executable, "-m", "aplite"])
        return

    load()
    if target.startswith("archipelago://"):
        clients, text_client = Launcher.handle_uri(target)
        if not clients:
            component = text_client
        elif len(clients) == 1:
            component = clients[0]
        else:
            choice = launcher.pick([*clients, text_client], target)
            component = choice[0] if choice else None
        if component:
            run(component, target, *component_args)
        return

    file, component = LauncherComponents.identify(target)
    if not component:
        sys.exit(f"Unknown component, patch or game: {target}")  # Launcher.main would open the kivy GUI
    run(component, *((file,) if file else ()), *component_args)


def run(component: "LauncherComponents.Component", *args: str) -> None:
    """Run a component in this process, so it gets the terminal and aplite's patches."""
    from worlds.LauncherComponents import export_datapackage, generate_yamls, open_host_yaml
    if component.func is open_host_yaml:  # upstream's starts a desktop editor in the background
        from aplite import settings_editor
        settings_editor.run()
    elif launcher.is_options_creator(component):
        from aplite import options_creator
        options_creator.run()
    elif component.func is export_datapackage:
        _export_datapackage()
    elif component.func is generate_yamls:
        _generate_templates(*args)
    elif component.func:
        # some clients parse sys.argv themselves and/or take no func args (e.g. civ6, kh1)
        sys.argv = [sys.argv[0], *args]
        Launcher.run_component(component, *(args if _accepts_args(component.func) else ()))
    elif component.script_name and component.cli:
        # command-line scripts (Generate, Host) need a process without worlds loaded; same terminal, foreground
        import subprocess
        script = Utils.local_path(f"{component.script_name}.py")
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [Utils.local_path(),
                                                                         os.environ.get("PYTHONPATH")]))}
        sys.exit(subprocess.call([sys.executable, "-m", "aplite.script", script, *args], env=env))
    elif launcher.runnable(component):
        # client scripts (SNIClient, ...) would otherwise be a new process without the patches
        import runpy
        script = Utils.local_path(f"{component.script_name}.py")
        sys.argv = [script, *args]
        runpy.run_path(script, run_name="__main__")
    else:
        sys.exit(f"{component.display_name} needs the full Archipelago GUI and isn't supported by aplite.")


def _export_datapackage() -> None:
    """Upstream's opens the file with xdg-open and asserts if there isn't one; this shows the path instead."""
    import json
    from aplite.desktop import open_path
    from worlds import network_data_package
    path = Utils.user_path("datapackage_export.json")
    with open(path, "w") as f:
        json.dump(network_data_package, f, indent=4)
    print(f"Wrote the data package for {len(network_data_package['games'])} games to:\n  {path}")
    if open_path(path):
        print("(opened it with your default app)")


def _generate_templates(*args: str) -> None:
    """Upstream's opens a file manager at the end; this shows the folder (and opens it when there's a desktop)."""
    from aplite.desktop import open_path
    from worlds.LauncherComponents import generate_yamls
    print("Generating template YAMLs for every installed game…")
    generate_yamls("--skip_open_folder", *args)
    folder = Utils.user_path("Players", "Templates")
    print(f"Templates are in:\n  {folder}")
    if "--skip_open_folder" not in args and open_path(folder):
        print("(opened the folder)")


def _accepts_args(func) -> bool:
    import inspect
    try:
        params = inspect.signature(func).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL) for p in params)


if __name__ == "__main__":
    main()
