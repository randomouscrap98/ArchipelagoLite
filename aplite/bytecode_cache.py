"""
Cache compiled code for modules imported from zips (.apworld files).

Python can't write .pyc files into zips, so every apworld module is recompiled on every start (~1.8s for 32 apworlds).
This swaps zipimport's compile step for one that stores code objects in the Archipelago cache dir, keyed by the
module path, its source and the Python bytecode version, so edited or updated apworlds recompile automatically.
"""
import hashlib
import importlib.util
import logging
import marshal
import os
import zipimport

import Utils


def install() -> None:
    cache_dir = Utils.cache_path("aplite", "bytecode")
    original = zipimport._compile_source

    def compile_source(pathname: str, source: bytes):
        digest = hashlib.sha256(importlib.util.MAGIC_NUMBER + pathname.encode() + b"\0" + source).hexdigest()
        path = os.path.join(cache_dir, digest)
        try:
            with open(path, "rb") as f:
                return marshal.load(f)
        except FileNotFoundError:
            pass
        except Exception as e:  # corrupt entry; recompile and overwrite
            logging.debug(f"Bad bytecode cache entry {path}: {e}")
        code = original(pathname, source)
        try:
            os.makedirs(cache_dir, exist_ok=True)
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                marshal.dump(code, f)
            os.replace(tmp, path)  # atomic, so concurrent starts never read half a file
        except OSError as e:
            logging.debug(f"Could not write bytecode cache {path}: {e}")
        return code

    zipimport._compile_source = compile_source
