"""Process-wide stat-keyed value cache for per-run-dir file reads on the warm engine's hot paths.

`cached(namespace, path, compute)` returns the stored value while the file's (st_mtime_ns, st_size)
is unchanged and calls `compute(path)` otherwise. `drop` forgets one entry.

`record_dir(root, run_id)` is `ask_contract.record_dir` memoized per (root, run_id) while the run's
`state/warp/<run_id>` directory keeps its (st_dev, st_ino); it skips the two `resolve()` calls and
the `is_dir` probe a 5000-run scan otherwise pays per run. A run dir absent from `state/warp`
(scratch-only, or not yet created) is answered from a process-wide entry that two `stat`s re-validate
on every call (state dir still absent, scratch dir's identity and mtime unchanged), so it is never
served stale.

Trap: a rewrite that keeps the size and lands inside one filesystem mtime tick is served stale.
No current writer calls `drop`; every rewrite is only seen once mtime or size moves.
"""
from __future__ import annotations

import os
import stat
import threading
from collections import OrderedDict
from typing import Any, Callable, Optional
from pathlib import Path

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
            _ABSENT_DIRS.clear()
        else:
            _CACHES.pop(namespace, None)


def size(namespace: str) -> int:
    """Entry count held for `namespace`."""
    with _LOCK:
        return len(_CACHES.get(namespace, ()))


_RECORD_DIRS: "OrderedDict[tuple, tuple]" = OrderedDict()  # (root, run_id) -> ((st_dev, st_ino), Path)
_ABSENT_DIRS: "OrderedDict[tuple, tuple]" = OrderedDict()  # (root, run_id) -> (scratch signature, Path)


class _NotCacheable(Exception):
    pass


def record_dir(repo_root, run_id) -> Path:
    """`ask_contract.record_dir(repo_root, run_id)`, reused while the state run dir keeps its identity.

    Inside a `call_io_scope` a present run dir is resolved once per scope (no re-stat): a claim
    pass calls this ~12 times per run. An absent dir is never memoized, so a dir created mid-scope
    is still seen. Raises ValueError for an unsafe run_id exactly as `ask_contract.record_dir` does."""
    from coordinator_core.git import call_io_scope as _cio
    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit import ask_contract

    if not isinstance(run_id, str) or not safe_id(run_id):
        return ask_contract.record_dir(repo_root, run_id)
    key = (os.fspath(repo_root), run_id)
    try:
        if not _cio.scope_open():
            return _identity_cached(key, ask_contract)
        return _cio.memo("run-record-dir", key[0] + "\0" + run_id, lambda: _identity_cached(key, ask_contract))
    except _NotCacheable:
        return _absent_resolved(key, ask_contract)


def _scratch_signature(root: str, run_id: str, ask_contract):
    """(st_dev, st_ino, st_mtime_ns) of the scratch run dir, None when it is absent: its mtime moves when a
    record file is created or removed in it, which is all `ask_contract._record_dir` selects scratch on."""
    try:
        st = os.stat(os.path.join(root, ask_contract.RUN_DIR_ROOT, run_id))
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns)


def _absent_resolved(key, ask_contract) -> Path:
    """`ask_contract._record_dir` for a run with no state dir, reused while the state dir stays absent and the
    scratch dir's signature holds. Trap: a state dir that appears is seen at once (the `stat` succeeds)."""
    root, run_id = key
    state = os.path.join(root, ask_contract.RECORD_DIR_ROOT, run_id)
    try:
        os.stat(state)
    except OSError:
        pass
    else:
        return ask_contract._record_dir(Path(root), run_id)
    sig = _scratch_signature(root, run_id, ask_contract)
    with _LOCK:
        hit = _ABSENT_DIRS.get(key)
        if hit is not None and hit[0] == sig:
            _ABSENT_DIRS.move_to_end(key)
            return hit[1]
    value = ask_contract._record_dir(Path(root), run_id)
    with _LOCK:
        _ABSENT_DIRS[key] = (sig, value)
        _ABSENT_DIRS.move_to_end(key)
        while len(_ABSENT_DIRS) > MAX_ENTRIES_PER_NAMESPACE:
            _ABSENT_DIRS.popitem(last=False)
    return value


def _identity_cached(key, ask_contract) -> Path:
    """Resolved record dir for a present state run dir, memoized on its (st_dev, st_ino); raises _NotCacheable otherwise."""
    root, run_id = key
    try:
        st = os.stat(os.path.join(root, ask_contract.RECORD_DIR_ROOT, run_id))
    except OSError:
        raise _NotCacheable from None
    if not stat.S_ISDIR(st.st_mode) or not st.st_ino:
        raise _NotCacheable
    ident = (st.st_dev, st.st_ino)
    with _LOCK:
        hit = _RECORD_DIRS.get(key)
        if hit is not None and hit[0] == ident:
            _RECORD_DIRS.move_to_end(key)
            return hit[1]
    value = ask_contract._record_dir(Path(root), run_id)
    with _LOCK:
        _RECORD_DIRS[key] = (ident, value)
        _RECORD_DIRS.move_to_end(key)
        while len(_RECORD_DIRS) > MAX_ENTRIES_PER_NAMESPACE:
            _RECORD_DIRS.popitem(last=False)
    return value
