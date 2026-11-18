"""Process-wide stat-keyed value cache for per-run-dir file reads on the warm engine's hot paths.

`cached(namespace, path, compute)` returns the stored value while the file's (st_mtime_ns, st_size)
is unchanged and calls `compute(path)` otherwise. Writers inside the engine call `drop`.

Trap: a rewrite that keeps the size and lands inside one filesystem mtime tick is served stale.
In-engine writers must `drop`; out-of-engine writers are only seen once mtime or size moves.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from typing import Any, Callable, Optional

# Above the ~5k run dirs under state/warp so one full scan does not evict its own head.
MAX_ENTRIES_PER_NAMESPACE = 8192

_LOCK = threading.Lock()
_CACHES: dict = {}  # namespace -> OrderedDict[abs path -> (st_mtime_ns, st_size, value)], LRU order


def cached(namespace: str, path, compute: Callable[[str], Any], st: Optional[os.stat_result] = None) -> Any:
    """Value of `compute(path)`, reused while the file's (mtime_ns, size) is unchanged.

    `st` is a stat the caller already holds (a scandir entry's). A missing file returns
    `compute(path)` uncached and evicts any stale entry. `compute` runs outside the lock; an
    exception from it propagates and nothing is stored."""
    key = os.fspath(path)
    try:
        st = st if st is not None else os.stat(key)
    except OSError:
        drop(namespace, key)
        return compute(key)
    sig = (st.st_mtime_ns, st.st_size)
    with _LOCK:
        cache = _CACHES.get(namespace)
        if cache is not None:
            hit = cache.get(key)
            if hit is not None and hit[:2] == sig:
                cache.move_to_end(key)
                return hit[2]
    value = compute(key)
    with _LOCK:
        cache = _CACHES.setdefault(namespace, OrderedDict())
        cache[key] = (*sig, value)
        cache.move_to_end(key)
        while len(cache) > MAX_ENTRIES_PER_NAMESPACE:
            cache.popitem(last=False)
    return value


def drop(namespace: str, path) -> None:
    """Forget `path` in `namespace`; writers call this after rewriting or removing the file."""
    with _LOCK:
        cache = _CACHES.get(namespace)
        if cache is not None:
            cache.pop(os.fspath(path), None)


def clear(namespace: Optional[str] = None) -> None:
    """Empty one namespace, or all when None."""
    with _LOCK:
        if namespace is None:
            _CACHES.clear()
        else:
            _CACHES.pop(namespace, None)


def size(namespace: str) -> int:
    """Entry count held for `namespace`."""
    with _LOCK:
        return len(_CACHES.get(namespace, ()))
