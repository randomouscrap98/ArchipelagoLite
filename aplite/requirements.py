"""
The "no kivy" rule for Archipelago's requirements, shared by aplite.sh (installing) and aplite/__main__.py (the
startup requirements check). Standard library only, so aplite.sh can run it before anything is installed.
Usage: python aplite/requirements.py REQUIREMENTS.txt   (prints the lines aplite installs)
"""
import re
import sys

KIVY = re.compile(r"\s*kivy", re.IGNORECASE)  # kivy and kivymd: the GUI aplite replaces


def lite_requirements(path: str) -> list[str]:
    with open(path) as f:
        return [line for line in f if not KIVY.match(line)]


if __name__ == "__main__":
    sys.stdout.writelines(lite_requirements(sys.argv[1]))
