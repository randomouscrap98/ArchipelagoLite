"""Launcher screen for Open Patch: pick a patch file; dismisses with its path, or None to go back."""
from __future__ import annotations

import Utils
from aplite.file_picker import FilePicker


def patch_suffixes() -> list[str]:
    """Extensions of files some client can open; same list as upstream's open_patch."""
    from worlds.LauncherComponents import SuffixIdentifier, Type, components
    return sorted({suffix for c in components
                   if c.type == Type.CLIENT and isinstance(c.file_identifier, SuffixIdentifier)
                   for suffix in c.file_identifier.suffixes})


def patch_picker() -> FilePicker:
    return FilePicker("Open patch", patch_suffixes(), "patch_dir", Utils.local_path())
