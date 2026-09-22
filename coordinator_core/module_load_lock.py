"""module_load_lock.py — per-module-name mutual exclusion for the
dynamic-import-by-path loaders that must register a freshly created module
in `sys.modules` BEFORE calling `exec_module` on it.

Four sites (`baton_assemble.apply._load_doc_new_module`,
`ops.plan_tasks_mutate._load_harvest_module`,
`session.claims.handoff_lifecycle`, `publish.time_transform.
_import_seed_module`) each load a `coordinator/bin*` script by file path via
`importlib.util.spec_from_file_location` and register the module object in
`sys.modules[module_name]` ahead of `exec_module` — required so a dataclass/
typing/NamedTuple/Enum construct in the loaded script's own module-level
code, which resolves `sys.modules[cls.__module__]` during class-body
execution, does not crash with an unrelated `AttributeError`. Under a warm
engine serving concurrent dispatches on shared threads, that ordering opens
a window: a second concurrent caller for the SAME module_name can build and
register its own fresh module object over the first caller's still-
executing one, so a reader of `sys.modules[module_name]` during that window
can observe a half-executed module. See
state/bug-backlog/2026-08-16-sys-modules-published-before-exec-module-3e43d1a2a316.yaml.

`held_during_load(module_name)` is a per-name lock a caller wraps around its
own "check cache, build spec, register in sys.modules, exec_module, update
cache" sequence (double-checked: re-check the cache immediately after
acquiring the lock, since a concurrent caller may have finished the load
while this one was waiting). Two callers racing DIFFERENT module_names never
block each other — only two callers of the SAME name ever contend.

Negative-spec:
    - Does NOT itself touch `sys.modules`, build an `importlib` spec, or
      cache a loaded module — purely a mutual-exclusion primitive; every
      caller keeps its own cache and its own load sequence.
    - Does NOT serialize unrelated module names against each other — the
      registry holds one `threading.Lock` per distinct name, never a single
      global lock across all loaders.
    - Does NOT release or clear entries from the internal registry — a lock
      object, once created for a name, lives for the process; this trades an
      unbounded (but tiny — one lock per distinct loader module_name, a
      fixed small set in practice) registry for never re-deriving a name's
      identity across calls.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

_REGISTRY_LOCK = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


def _lock_for(module_name: str) -> threading.Lock:
    with _REGISTRY_LOCK:
        lock = _LOCKS.get(module_name)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[module_name] = lock
        return lock


@contextmanager
def held_during_load(module_name: str) -> Iterator[None]:
    """Blocks a second concurrent caller for the same `module_name` until
    the first caller's `with` block exits, so at most one thread is ever
    mid-way through registering-then-executing a module under that name.

    The caller is responsible for re-checking its own cache immediately
    after entry (a concurrent caller may have already finished the load
    while this one waited for the lock) — this primitive only serializes,
    it never memoizes.
    """
    lock = _lock_for(module_name)
    with lock:
        yield
