"""Process-wide stat-keyed value cache for per-run-dir file reads on the warm engine's hot paths.

`cached(namespace, path, compute)` returns the stored value while the file's (st_mtime_ns, st_size)
is unchanged and calls `compute(path)` otherwise. `drop` forgets one entry.

`record_dir(root, run_id)` is `ask_contract.record_dir` memoized per (root, run_id) while the run's
`state/warp/<run_id>` directory keeps its (st_dev, st_ino); it skips the two `resolve()` calls and
the `is_dir` probe a 5000-run scan otherwise pays per run. A run dir absent from `state/warp`
(scratch-only, or not yet created) is never cached.

Trap: a rewrite that keeps the size and lands inside one filesystem mtime tick is served stale.
No current writer calls `drop`; every rewrite is only seen once mtime or size moves.
"""
from __future__ import annotations

import os
import stat
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
    """Forget `path` in `namespace`: a writer that rewrites at constant size inside one mtime tick must call this."""
    with _LOCK:
        cache = _CACHES.get(namespace)
        if cache is not None:
            cache.pop(os.fspath(path), None)


def clear(namespace: Optional[str] = None) -> None:
    """Empty one namespace, or all when None."""
    with _LOCK:
        if namespace is None:
            _CACHES.clear()
            _RECORD_DIRS.clear()
        else:
            _CACHES.pop(namespace, None)


def size(namespace: str) -> int:
    """Entry count held for `namespace`."""
    with _LOCK:
        return len(_CACHES.get(namespace, ()))


_RECORD_DIRS: "OrderedDict[tuple, tuple]" = OrderedDict()  # (root, run_id) -> ((st_dev, st_ino), Path)


def record_dir(repo_root, run_id):
    """`ask_contract.record_dir(repo_root, run_id)`, reused while the state run dir keeps its identity.

    Raises ValueError for an unsafe run_id exactly as `ask_contract.record_dir` does."""
    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit import ask_contract

    if not isinstance(run_id, str) or not safe_id(run_id):
        return ask_contract.record_dir(repo_root, run_id)
    key = (os.fspath(repo_root), run_id)
    try:
        st = os.stat(os.path.join(key[0], ask_contract.RECORD_DIR_ROOT, run_id))
    except OSError:
        return ask_contract.record_dir(repo_root, run_id)
    if not stat.S_ISDIR(st.st_mode) or not st.st_ino:
        return ask_contract.record_dir(repo_root, run_id)
    ident = (st.st_dev, st.st_ino)
    with _LOCK:
        hit = _RECORD_DIRS.get(key)
        if hit is not None and hit[0] == ident:
            _RECORD_DIRS.move_to_end(key)
            return hit[1]
    value = ask_contract.record_dir(repo_root, run_id)
    with _LOCK:
        _RECORD_DIRS[key] = (ident, value)
        _RECORD_DIRS.move_to_end(key)
        while len(_RECORD_DIRS) > MAX_ENTRIES_PER_NAMESPACE:
            _RECORD_DIRS.popitem(last=False)
    return value
