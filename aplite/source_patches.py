"""
Machinery for source patches; the patches themselves are PATCHES in aplite/fixes/fixes.py. Source patches for apworld
modules: an exact snippet in one module replaced before it's compiled (bytecode_cache's zipimport hook, so zipped
.apworld files only). For world code that's slow or broken without kivy where no runtime fix
fits, e.g. work done while the module is imported.

A patch applies only while its whole `old` snippet appears exactly once, so a world update that touches that code
(a fix, a rewrite) turns it off by itself, and unrelated updates keep it. Match the full block whose behavior is being
replaced, not one line. The replacement must do the same thing. Remove an entry once the world fixes its code.
"""
from __future__ import annotations

import dataclasses
import logging

logger = logging.getLogger("aplite")


@dataclasses.dataclass(frozen=True)
class Patch:
    module: str
    """Path inside the apworld, e.g. osu/Items.py."""
    why: str
    """The world bug (and its cost), so the entry can be dropped once fixed."""
    old: str
    new: str
    versions: tuple[str, ...] = ()
    """For the crash notice only (as Fix.versions): world_version values this patch was made for; empty: all."""


PATCHES: list[Patch] = []
"""The patches in use, from the active fixes file (aplite/fixes/fixes.py; loaded by client_fixes.load())."""


def apply(pathname: str, source: bytes) -> bytes:
    """`source` of the module at `pathname` (zipimport's: ...apworld/osu/Items.py), patched where a patch matches."""
    path = pathname.replace("\\", "/")
    for patch in PATCHES:
        if not path.endswith("/" + patch.module):
            continue
        text = source.decode("utf-8").replace("\r\n", "\n")  # compile() normalizes line endings anyway
        if text.count(patch.old) != 1:
            logger.debug(f"source patch for {patch.module} doesn't match (world changed?); not applied")
            continue
        source = text.replace(patch.old, patch.new).encode("utf-8")
        logger.debug(f"source patch applied to {pathname}")
    return source
