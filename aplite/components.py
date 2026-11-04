"""
How aplite runs launcher components: the one place that decides what happens when one is picked or launched.

Most components run as upstream defines them (see runner()). OVERRIDES replaces or extends upstream's behavior for
the few that need a desktop GUI or would otherwise misbehave in a terminal; add new special cases there.
"""
from __future__ import annotations

import dataclasses
import functools
import inspect
import os
import subprocess
import sys
import typing

import Utils

import aplite

if typing.TYPE_CHECKING:
    from textual.screen import Screen
    from worlds.LauncherComponents import Component

Run = typing.Callable[..., None]


@dataclasses.dataclass(frozen=True)
class Override:
    run: Run | None = None
    """Replaces upstream's behavior; called with the component's args. None: run as upstream defines it."""
    picker: typing.Callable[[], Screen] | None = None
    """In the launcher, a screen (e.g. a FilePicker) shown first; its result goes to `picked`."""
    picked: typing.Callable[[Component, typing.Any], tuple[Component, tuple[str, ...]] | str] | None = None
    """Turns the picker's result into (component to run, args), or an error message to show."""
    pauses: bool = False
    """Its terminal output matters: wait for Enter before going back to the launcher."""
    returns: bool | None = None
    """Back to the launcher afterwards? None: the default rule (everything but clients)."""


# handlers


def _export_datapackage(*args: str) -> None:
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


def _open_patch(*args: str) -> None:
    """Direct launch (outside the launcher): upstream's would start a new kivy launcher process for the patch."""
    from aplite import prompts
    from aplite.patch_picker import patch_suffixes
    from worlds.LauncherComponents import identify
    path = args[0] if args else prompts.open_filename("Select patch", [("Patches", patch_suffixes())])
    if not path:
        return
    file, component = identify(path)
    if not component:
        sys.exit(f"No client can open {path}")
    run(component, file, *args[1:])


def _patch_picker() -> Screen:
    from aplite.patch_picker import patch_picker
    return patch_picker()


def _patch_picked(component: Component, path) -> tuple[Component, tuple[str, ...]] | str:
    from worlds.LauncherComponents import identify
    file, client = identify(str(path))
    if client and runnable(client):
        return client, (file,)
    return f"No client can open {path.name}"


def _apworld_picker() -> Screen:
    from aplite.file_picker import FilePicker
    return FilePicker("Install APWorld", [".apworld"], "apworld_dir", os.path.expanduser("~"))


def _settings_editor(*args: str) -> None:
    from aplite import settings_editor
    settings_editor.run()


def _options_creator(*args: str) -> None:
    from aplite import options_creator
    options_creator.run()


@functools.cache
def overrides() -> dict[typing.Callable | str, Override]:
    """Keyed by upstream's component func, or by script_name for script components. Imported lazily: importing
    worlds.LauncherComponents loads every world."""
    from worlds.LauncherComponents import export_datapackage, generate_yamls, install_apworld, open_host_yaml, \
        open_patch
    from aplite import webhost
    return {
        webhost.run: Override(picker=webhost.picker, picked=lambda component, path: (component, (str(path),))),
        open_patch: Override(run=_open_patch, picker=_patch_picker, picked=_patch_picked,
                             returns=True),  # the patch's client comes back to the launcher (user request)
        install_apworld: Override(picker=_apworld_picker, picked=lambda component, path: (component, (str(path),)),
                                  pauses=True),
        open_host_yaml: Override(run=_settings_editor),  # upstream's starts a desktop editor in the background
        "OptionsCreator": Override(run=_options_creator),  # upstream's is kivy
        export_datapackage: Override(run=_export_datapackage, pauses=True),
        generate_yamls: Override(run=_generate_templates, pauses=True),
    }


def override_for(component: Component) -> Override:
    table = overrides()
    return table.get(component.func) or table.get(component.script_name) or Override()


def extra_components() -> list[Component]:
    """aplite's own launcher entries."""
    from aplite import generate_gui, web_options, webhost
    from aplite.selfhost import app as selfhost_app
    return [web_options.component(), generate_gui.component(), webhost.component(), selfhost_app.component()]


# running


def runner(component: Component) -> Run | None:
    """How aplite runs this component, or None if it can't (it needs the full GUI). Everything runs in this process,
    so it gets the terminal and aplite's patches, except command-line scripts (see _run_cli_script)."""
    from worlds.LauncherComponents import Type
    override = override_for(component)
    if override.run:
        return override.run
    if component.func:
        run = lambda *args: _run_func(component, *args)  # noqa: E731
    elif component.script_name and component.cli:
        return lambda *args: _run_cli_script(component, *args)
    elif component.script_name and component.type == Type.CLIENT:
        return lambda *args: _run_client_script(component, *args)
    elif component.script_name and component.type == Type.ADJUSTER:
        run = lambda *args: _run_gui_script(component, *args)  # noqa: E731
    else:
        return None
    if component.type != Type.ADJUSTER:
        return run

    def with_notice(*args: str) -> None:  # adjusters are desktop (tkinter) windows; the terminal just waits
        print(f"{component.display_name} is open in its own window. Close it to return to the launcher.",
              flush=True)
        run(*args)
    return with_notice


def runnable(component: Component) -> bool:
    return runner(component) is not None


def run(component: Component, *args: str) -> None:
    how = runner(component)
    if how is None:
        sys.exit(f"{component.display_name} needs the full Archipelago GUI and isn't supported by aplite.")
    how(*args)


def returns_to_launcher(component: Component) -> bool:
    """Everything but clients goes back to the launcher when it's done; exiting a client means you're done."""
    from worlds.LauncherComponents import Type
    returns = override_for(component).returns
    return component.type != Type.CLIENT if returns is None else returns


def pauses_after(component: Component) -> bool:
    return override_for(component).pauses or bool(component.cli)


def _run_func(component: Component, *args: str) -> None:
    import Launcher
    # some clients parse sys.argv themselves and/or take no func args (e.g. civ6, kh1)
    sys.argv = [sys.argv[0], *args]
    Launcher.run_component(component, *(args if _accepts_args(component.func) else ()))


def _script(component: Component) -> str:
    return Utils.local_path(f"{component.script_name}.py")


def _run_cli_script(component: Component, *args: str) -> None:
    """Command-line scripts (Generate, Host) need a process without worlds loaded (Generate.py refuses otherwise):
    a child process in the same terminal, via aplite.script. Its user quitting (ctrl+q in a file picker) quits
    aplite too."""
    from aplite import nav
    status = subprocess.call([sys.executable, "-m", "aplite.script", _script(component), *args],
                             env=aplite.child_env())
    sys.exit(0 if nav.quit_requested(status) else status)


def _run_gui_script(component: Component, *args: str) -> None:
    """Desktop tool scripts (LttP/OoT Adjuster: tkinter): a child process like upstream's, without aplite's terminal
    prompts so their own file dialogs show. Waits for the window to close."""
    status = subprocess.call([sys.executable, _script(component), *args], env=aplite.child_env())
    if status:  # e.g. no display: keep the error on screen
        aplite.wait_for_enter(f"{component.display_name} failed (exit {status}). Press Enter to return to the "
                              f"launcher…")
    sys.exit(status)


def _run_client_script(component: Component, *args: str) -> None:
    """Client scripts (SNIClient, ...): upstream starts them as a new process, which wouldn't have aplite's patches."""
    import runpy
    script = _script(component)
    sys.argv = [script, *args]
    runpy.run_path(script, run_name="__main__")


def _accepts_args(func: typing.Callable) -> bool:
    try:
        params = inspect.signature(func).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL) for p in params)
