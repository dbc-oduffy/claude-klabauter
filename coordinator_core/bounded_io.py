"""Per-key deadline runner for best-effort IO on an op's hot path.

``run_bounded`` runs a callable on a daemon thread and waits at most ``deadline``:
a counter, advisory or degradable read that a saturated disk wedges is dropped
(the caller gets ``default``) instead of holding the op to its own budget. The
shape is ``atomic_append.append_line``'s: stall-marking per key, one stderr line,
immediate drop on a stalled key until its worker returns.

Not for IO whose completion or ordering the caller needs (read-modify-write that
must land): there a drop is a lost update, not a lost counter.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Callable, NamedTuple, Optional, TypeVar

from coordinator_core.atomic_append import APPEND_DEADLINE_SECS

T = TypeVar("T")

#: Deadline for ``read_bytes_bounded``; sits under the 500ms brightline and is not a dial.
READ_DEADLINE_SECS = 0.25

_lock = threading.Lock()
_stalled: "dict[str, _Job]" = {}
_inflight: "dict[str, _Job]" = {}
_open_lock = threading.Lock()
_open_paths: "dict[int, tuple[str, float]]" = {}
_TIMED_OUT = object()


class IoIndeterminate(Exception):
    """A bounded read did not finish: the file's state is unknown, not absent.

    Deliberately not an ``OSError``: ``OSError`` catch sites map to absence, and
    a stalled open must never read as "file missing".
    """

    def __init__(self, path: str, waited_ms: int) -> None:
        super().__init__(f"read of {path} indeterminate after {waited_ms}ms")
        self.path = path
        self.waited_ms = waited_ms


class OpenPath(NamedTuple):
    """One open/read in flight: ``t0`` is ``time.monotonic()`` at its start."""

    path: str
    t0: float

    @property
    def age_ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)


class _Job:
    __slots__ = ("done", "result", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: object = None
        self.error: "BaseException | None" = None


def _run(key: str, fn: Callable[[], object], job: _Job) -> None:
    try:
        job.result = fn()
    except BaseException as exc:  # noqa: BLE001 - handed to the waiting caller
        job.error = exc
    finally:
        with _lock:
            if _stalled.get(key) is job:
                del _stalled[key]
            if _inflight.get(key) is job:
                del _inflight[key]
            job.done.set()


def run_bounded(
    key: str,
    fn: Callable[[], T],
    *,
    deadline: float = APPEND_DEADLINE_SECS,
    default: T,
) -> T:
    """Return ``fn()``, or ``default`` if it has not finished within ``deadline``.

    ``key`` names the resource (e.g. the sink path): once a call on ``key``
    overruns, the key is stalled and later calls return ``default`` at once
    without running ``fn`` until the overrun worker returns. An exception raised
    by ``fn`` inside the deadline is re-raised here; one raised after the
    deadline is discarded with the dropped result. The overrun worker keeps
    running, so ``fn`` must be safe to complete late.
    """
    job = _Job()
    with _lock:
        if key in _stalled:
            return default
        prior = _inflight.get(key)
        if prior is None:
            _inflight[key] = job
    if prior is not None:
        # A worker is already running on this key: wait on it rather than leak a second one.
        if not prior.done.wait(deadline):
            with _lock:
                if not prior.done.is_set():
                    _stalled[key] = prior
            return default
        return run_bounded(key, fn, deadline=deadline, default=default)
    threading.Thread(
        target=_run, args=(key, fn, job), daemon=True, name=f"bounded-io:{key}"
    ).start()
    if job.done.wait(deadline):
        if job.error is not None:
            raise job.error
        return job.result  # type: ignore[return-value]
    with _lock:
        first = not job.done.is_set()
        if first:
            _stalled[key] = job
    if first:
        try:
            sys.stderr.write(f"bounded_io: stalled past {deadline}s, dropped: {key}\n")
        except Exception:
            pass
        return default
    if job.error is not None:
        raise job.error
    return job.result  # type: ignore[return-value]


def inflight_open_paths() -> "list[OpenPath]":
    """Paths with an open/read in flight, oldest first. A dict copy under a lock: no I/O, never raises."""
    with _open_lock:
        entries = [OpenPath(path, t0) for path, t0 in _open_paths.values()]
    entries.sort(key=lambda entry: entry.t0)
    return entries


def _read_registered(path: str) -> Optional[bytes]:
    ident = threading.get_ident()
    with _open_lock:
        _open_paths[ident] = (path, time.monotonic())
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except (FileNotFoundError, NotADirectoryError):
        return None
    finally:
        with _open_lock:
            _open_paths.pop(ident, None)


def read_bytes_bounded(path: str, *, deadline: float = READ_DEADLINE_SECS) -> Optional[bytes]:
    """Return the file's bytes, ``None`` when it is absent, or raise ``IoIndeterminate``.

    ``IoIndeterminate`` when the open/read has not finished within ``deadline``,
    or when an earlier read of the same path is still wedged. Other ``OSError``s
    from the read propagate.

    Trap: a blocked CreateFile cannot be cancelled, so the overrun worker thread
    leaks until the OS returns it, at most one per path (``run_bounded`` keeps the
    key stalled meanwhile). Its ``inflight_open_paths`` entry stays until then.
    """
    t0 = time.monotonic()
    key = f"read:{path}"
    result = run_bounded(key, lambda: _read_registered(path), deadline=deadline, default=_TIMED_OUT)
    if result is _TIMED_OUT:
        raise IoIndeterminate(path, int((time.monotonic() - t0) * 1000))
    return result  # type: ignore[return-value]
