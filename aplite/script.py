"""
Run a command-line Archipelago script (Generate, MultiServer) in a fresh process, with aplite's bytecode cache and
terminal file prompts but before any worlds load (Generate refuses to start if they already are).
Usage: python -m aplite.script [--gui] SCRIPT.py [args]   (with aplite.child_env(): repo importable, no ModuleUpdate)
--gui: desktop tools (LttP/OoT Adjuster) keep their own file dialogs (no terminal prompts). Importing aplite still
points user data at APLITE_DATA_DIR (datadir), which running the script directly would skip.
"""
import builtins
import os
import runpy
import sys

from aplite import bytecode_cache, prompts

gui = sys.argv[1:2] == ["--gui"]
if gui:
    del sys.argv[1]
bytecode_cache.install()
if not gui:
    prompts.install()

if os.environ.get(prompts.RELAY_ENV):  # stdin is the relay pipe: Generate's "Press enter to close." mustn't wait on it
    builtins.input = lambda prompt="": print(prompt) or ""

script = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(script, run_name="__main__")
