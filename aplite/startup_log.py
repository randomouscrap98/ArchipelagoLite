"""
Startup lines for the aplite log, so a slow-start or bug report comes with data (other machines take 10-20s to
load). Upstream's first line already has the OS and Python version.
"""
import logging
import os
import sys


def install_type() -> str:
    from aplite.version import VERSION_FILE
    if not os.path.isfile(VERSION_FILE):  # build_dist writes it; a checkout has git instead
        return "source"
    if os.environ.get("APPIMAGE"):
        return "AppImage"
    return "Windows zip" if sys.platform == "win32" else "Linux tar.gz"


def speedups() -> str:
    import NetUtils
    if NetUtils.LocationStore is NetUtils._LocationStore:
        return "not available (pure Python)"
    import _speedups
    return "pyximport build" if ".pyxbld" in _speedups.__file__ else "compiled"


def report(total: float) -> None:
    """After the worlds load: the setup, then how long the load took and which worlds were slowest. Per-world times
    are upstream's (WorldSource.time_taken) and include the first import of anything the world uses."""
    import worlds
    from aplite import bytecode_cache
    from aplite.version import version
    logging.info(f"aplite {version()} ({install_type()}), _speedups: {speedups()}")
    times = [(source.name, source.time_taken) for source in worlds.world_sources
             if source.time_taken >= 0]  # -1: not loaded (failed, or an older duplicate of a game)
    slowest = sorted(times, key=lambda item: item[1], reverse=True)[:5]
    logging.info(f"aplite: worlds loaded in {total:.2f}s ({len(times)} worlds, {sum(t for _, t in times):.2f}s in "
                 f"world imports; apworld code cache: {bytecode_cache.hits} hits, {bytecode_cache.compiled} compiled); "
                 f"slowest: " + ", ".join(f"{name} {seconds:.2f}s" for name, seconds in slowest))
