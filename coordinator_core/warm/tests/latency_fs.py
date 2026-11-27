"""Induced engine-root IO latency inside a REAL `ProcessPoolExecutor` worker.

`make_spec` builds the cross-process primitives; `worker_init` is the pool initializer that installs
the shim and then runs the production `server._worker_process_init`. While `spec.armed` is set, the
FIRST worker that touches a path under `spec.prefixes` through `open`, `io.open`, `os.open`, `os.stat`
or `os.replace` becomes the holder and parks on `spec.release` (a polled flag); every other
worker passes through. Python-level patching only, so the shim behaves the same on Windows and POSIX.
"""

from __future__ import annotations

import builtins
import contextlib
import io
import os
import time
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

#: Failsafe so a leaked holder cannot outlive a crashed test run.
HOLD_CAP_SECS = 300.0
_WARM_TIMEOUT_SECS = 60.0
_POLL_SECS = 0.02


class ReleaseFlag:
    """An Event-shaped flag over a lock-free shared byte, waited on by polling.

    trap: a worker terminated inside `multiprocessing.Event.wait` leaves a sleeper that never
    acknowledges, so the next `Event.set()` blocks forever; a polled flag has no sleeper to lose.
    """

    def __init__(self, mp_context: Any) -> None:
        self._flag = mp_context.Value("b", 0, lock=False)

    def set(self) -> None:
        self._flag.value = 1

    def clear(self) -> None:
        self._flag.value = 0

    def is_set(self) -> bool:
        return bool(self._flag.value)

    def wait(self, timeout: "float | None" = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._flag.value:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_SECS)
        return True


@dataclass
class LatencySpec:
    """Picklable only while a spawn context starts a worker (pass it through `initargs`)."""

    prefixes: tuple
    armed: Any
    entered: Any
    release: Any
    holder: Any
    lock: Any
    warm_barrier: Any


def _norm(path: Any) -> str:
    return os.path.normcase(os.path.abspath(os.fsdecode(path)))


def make_spec(mp_context: Any, prefixes: Sequence[Any], warm_workers: int) -> LatencySpec:
    """Primitives for a pool of `warm_workers` workers holding IO under `prefixes` (dirs or files)."""
    return LatencySpec(
        prefixes=tuple(_norm(p) for p in prefixes),
        armed=mp_context.Event(),
        entered=mp_context.Event(),
        release=ReleaseFlag(mp_context),
        holder=mp_context.Value("l", 0),
        lock=mp_context.Lock(),
        warm_barrier=mp_context.Barrier(warm_workers),
    )


_SPEC: "LatencySpec | None" = None
_INSTALLED = False


def _gate(path: Any) -> None:
    spec = _SPEC
    if spec is None or isinstance(path, int) or not spec.armed.is_set():
        return
    try:
        target = _norm(path)
    except (TypeError, ValueError):
        return
    if not target.startswith(spec.prefixes):
        return
    me = os.getpid()
    with spec.lock:
        if spec.holder.value == 0:
            spec.holder.value = me
        mine = spec.holder.value == me
    if mine:
        spec.entered.set()
        spec.release.wait(HOLD_CAP_SECS)


def install(spec: LatencySpec) -> None:
    """Wrap the IO entry points in this process. Idempotent."""
    global _SPEC, _INSTALLED
    _SPEC = spec
    if _INSTALLED:
        return
    _INSTALLED = True
    real_open, real_os_open, real_stat, real_replace = builtins.open, os.open, os.stat, os.replace

    def _open(file, *args, **kwargs):
        _gate(file)
        return real_open(file, *args, **kwargs)

    def _os_open(path, *args, **kwargs):
        _gate(path)
        return real_os_open(path, *args, **kwargs)

    def _stat(path, *args, **kwargs):
        _gate(path)
        return real_stat(path, *args, **kwargs)

    def _replace(src, dst, *args, **kwargs):
        _gate(src)
        _gate(dst)
        return real_replace(src, dst, *args, **kwargs)

    builtins.open = _open
    io.open = _open
    os.open = _os_open
    os.stat = _stat
    os.replace = _replace


def worker_init(spec: LatencySpec, stderr_dir: str, stall_dir: str) -> None:
    """Pool initializer: the shim first, then the production worker boot."""
    install(spec)
    from coordinator_core.warm import server

    server._worker_process_init(stderr_dir, stall_dir)


def _barrier_task() -> int:
    assert _SPEC is not None
    _SPEC.warm_barrier.wait(_WARM_TIMEOUT_SECS)
    return os.getpid()


def warm_pool(pool: Any, workers: int) -> set:
    """Boot all `workers` processes (a barrier admits none until every worker holds a task); their pids."""
    futures = [pool.submit(_barrier_task) for _ in range(workers)]
    return {f.result(timeout=_WARM_TIMEOUT_SECS * 2) for f in futures}


@contextlib.contextmanager
def hold_engine_root_io(spec: LatencySpec) -> Iterator[LatencySpec]:
    """Arm the shim; on exit release any holder and disarm."""
    spec.armed.set()
    try:
        yield spec
    finally:
        spec.release.set()
        spec.armed.clear()
