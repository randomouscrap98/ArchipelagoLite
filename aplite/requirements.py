"""
Requirements handling shared by aplite.sh and build_dist.sh (installing) and aplite/__main__.py (the startup check).
Standard library only, so the scripts can run it before anything is installed.
Usage: python aplite/requirements.py REQUIREMENTS.txt          the lines aplite installs (no kivy: the GUI it replaces)
       python aplite/requirements.py --each REQUIREMENTS.txt   one requirement per line, for installing them one by
                                                               one when the file fails as a whole
"""
import re
import sys

KIVY = re.compile(r"\s*kivy", re.IGNORECASE)  # kivy and kivymd: the GUI aplite replaces


def lite_requirements(path: str) -> list[str]:
    with open(path) as f:
        return [line for line in f if not KIVY.match(line)]


def each_requirement(path: str) -> list[str]:
    """The file's requirements without comments, hash lines and line continuations."""
    with open(path) as f:
        lines = [line.rstrip("\n") for line in f]
    return [requirement for line in lines if "hash=" not in line and not line.lstrip().startswith("#")
            if (requirement := line.rstrip(" \\").strip())]


if __name__ == "__main__":
    if sys.argv[1:2] == ["--each"]:
        print("\n".join(each_requirement(sys.argv[2])))
    else:
        sys.stdout.writelines(lite_requirements(sys.argv[1]))
