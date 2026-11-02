"""
Run a command-line Archipelago script (Generate, MultiServer) in a fresh process, with aplite's bytecode cache and
terminal file prompts but before any worlds load (Generate refuses to start if they already are).
Usage: python -m aplite.script SCRIPT.py [args]   (with the repo importable: see aplite.child_env)
"""
import os
import runpy
import sys

os.environ["SKIP_REQUIREMENTS_UPDATE"] = "1"  # aplite already handled requirements (and they'd include kivy)

from aplite import bytecode_cache, prompts

bytecode_cache.install()
prompts.install()

script = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(script, run_name="__main__")
