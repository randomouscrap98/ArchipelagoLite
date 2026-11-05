"""
aplite's version: <Archipelago version>-lite.N, from git tags `v<version>-lite.N` (`git describe`, so builds after a
tag read like 0.6.8-lite.1-3-gabc1234). build_dist.sh writes it to _version.txt, since packages have no git.
Usage (scripts; no dependencies needed): python -m aplite.version [--upstream]   (--upstream: Archipelago's version)
"""
import functools
import os
import re
import subprocess
import sys

from aplite import REPO

VERSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_version.txt")


def upstream_version() -> str:
    """Archipelago's __version__, read from Utils.py (importing Utils needs the dependencies)."""
    with open(os.path.join(REPO, "Utils.py")) as f:
        return re.search(r'^__version__ = "(.*)"', f.read(), re.M).group(1)


@functools.cache
def version() -> str:
    try:
        with open(VERSION_FILE) as f:
            return f.read().strip()
    except OSError:
        pass
    git = ["git", "-C", REPO]
    try:
        described = subprocess.run([*git, "describe", "--tags", "--match", "v*-lite.*"],
                                   capture_output=True, text=True).stdout.strip()
        if described:
            return described.removeprefix("v")
        commit = subprocess.run([*git, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    except OSError:  # no git
        commit = ""
    return f"{upstream_version()}-lite.0" + (f"-g{commit}" if commit else "")


if __name__ == "__main__":
    print(upstream_version() if sys.argv[1:] == ["--upstream"] else version())
