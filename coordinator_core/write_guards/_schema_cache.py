"""Process-level cache of parsed frontmatter schema corpora, shared by the deny and advisory guards.

A directory's entry is one immutable (signature, schemas) tuple replaced by a single assignment, so a
pool thread never pairs a signature with another load's dict; a racing duplicate load is accepted and
no lock is held across a load. The signature is a scandir of (name, mtime_ns, size), so an unchanged
corpus costs one directory enumeration and zero schema-file opens.
"""

from __future__ import annotations

import os
from typing import Any, Callable

_Signature = tuple[tuple[str, int, int], ...]
_CACHE: dict[str, tuple[_Signature, dict[str, Any]]] = {}


def _signature(schemas_dir: str) -> _Signature:
    rows = []
    with os.scandir(schemas_dir) as it:
        for e in it:
            if e.name.endswith((".yaml", ".schema.json")) and e.is_file():
                st = e.stat()
                rows.append((e.name, st.st_mtime_ns, st.st_size))
    return tuple(sorted(rows))


def _fresh_copy(schemas: dict[str, Any]) -> dict[str, Any]:
    """Copy the three containers the guards mutate; per-schema values stay shared and read-only."""
    out = dict(schemas)
    out["_byGlob"] = list(schemas["_byGlob"])
    out["_byKind"] = dict(schemas["_byKind"])
    return out


def cached_load_schemas(schemas_dir: str, loader: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    """Return a caller-owned copy of `loader(schemas_dir)`, re-invoking `loader` only when the dir signature changed.

    A signature or loader failure propagates, and nothing is stored on failure.
    """
    key = os.path.abspath(schemas_dir)
    sig = _signature(key)
    entry = _CACHE.get(key)
    if entry is not None and entry[0] == sig:
        return _fresh_copy(entry[1])
    loaded = loader(schemas_dir)
    _CACHE[key] = (sig, loaded)
    return _fresh_copy(loaded)


def clear() -> None:
    """Drop every cached corpus."""
    _CACHE.clear()
