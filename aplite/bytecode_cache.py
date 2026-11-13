"""
Cache compiled code for modules imported from zips (.apworld files).

Python can't write .pyc files into zips, so every apworld module is recompiled on every start (~1.8s for 32 apworlds).
This keeps one cache file for all apworlds (on Windows each file's first open after boot costs ~5ms, so 100+ files
cost ~0.6s) holding each module's code object, keyed by apworld path and the CRC32 and size the zip's directory already
lists: a hit needs no decompression, and an edited or updated module recompiles. The file is also keyed by the Python
bytecode version and aplite.source_patches (applied here). New entries are written by flush(): after the worlds load,
before a relaunch, and at exit; apworlds that no longer exist are dropped then.
"""
import atexit
import hashlib
import importlib.util
import logging
import marshal
import os
import threading
import zipimport

import Utils
from aplite import source_patches, write_atomic

_bundles: dict[str, dict[str, tuple[int, int, object]]] = {}
"""Per apworld path: {path inside the zip: (crc32, size, code)}, as loaded plus this run's compiles."""
_loaded = False
_dirty = False
_lock = threading.Lock()  # the launcher loads worlds in a thread while the main thread may flush
_cache_file = ""
hits = compiled = 0  # this run's apworld modules, for aplite.startup_log
_version = b""


def _bundle(archive: str) -> dict[str, tuple[int, int, object]]:
    """Call with _lock held. The cache file is read on first use."""
    global _loaded
    if not _loaded:
        _loaded = True
        try:
            with open(_cache_file, "rb") as f:
                version, bundles = marshal.loads(f.read())  # load(f) reads in small pieces: ~2.5x slower
            if version == _version:
                _bundles.update(bundles)
        except FileNotFoundError:
            pass
        except Exception as e:  # corrupt or from another format: rebuilt as modules compile
            logging.debug(f"Bad bytecode cache {_cache_file}: {e}")
    return _bundles.setdefault(os.path.abspath(archive), {})


def flush() -> None:
    """Write the cache file if it got new entries. Safe to call any time, from any thread."""
    global _dirty
    with _lock:
        if not _dirty:
            return
        _dirty = False
        bundles = {archive: dict(bundle) for archive, bundle in _bundles.items() if bundle and os.path.isfile(archive)}
    try:
        write_atomic(_cache_file, marshal.dumps((_version, bundles)))  # readers never see half a file
    except (OSError, ValueError) as e:
        logging.debug(f"Could not write bytecode cache {_cache_file}: {e}")


def install() -> None:
    global _cache_file, _version
    _cache_file = Utils.cache_path("aplite", "apworld-code.bin")
    patches = repr([(p.module, p.old, p.new) for p in source_patches.PATCHES]).encode()
    _version = importlib.util.MAGIC_NUMBER + hashlib.sha256(patches).digest()
    original_compile = zipimport._compile_source
    original_get_code = zipimport._get_module_code

    def compile_source(pathname: str, source: bytes):
        try:
            source = source_patches.apply(pathname, source)
        except Exception:  # a world's odd source (encoding): compile it as it is
            logging.exception(f"source patches failed for {pathname}")
        return original_compile(pathname, source)

    def get_module_code(self, fullname: str):
        global hits, compiled, _dirty
        path = zipimport._get_module_path(self, fullname)
        files = self._get_files()
        for suffix, isbytecode, ispackage in zipimport._zip_searchorder:
            entry = files.get(path + suffix)
            if entry is None:
                continue
            if isbytecode:  # a .pyc inside the zip: zipimport's own handling
                break
            # toc entry: (full path, compress, data size, file size, offset, time, date, crc)
            inner, size, crc = path + suffix, entry[3], entry[7]
            with _lock:
                cached = _bundle(self.archive).get(inner)
            if cached and cached[0] == crc and cached[1] == size:
                hits += 1
                return cached[2], ispackage, entry[0]
            code, ispackage, modpath = original_get_code(self, fullname)
            if modpath == entry[0]:  # compiled from this source (not a .pyc found first)
                compiled += 1
                with _lock:
                    _bundle(self.archive)[inner] = (crc, size, code)
                    _dirty = True
            return code, ispackage, modpath
        return original_get_code(self, fullname)

    zipimport._compile_source = compile_source
    zipimport._get_module_code = get_module_code
    atexit.register(flush)
