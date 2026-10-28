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
from typing import Callable, TypeVar

from coordinator_core.atomic_append import APPEND_DEADLINE_SECS

T = TypeVar("T")

_lock = threading.Lock()
_stalled: "dict[str, _Job]" = {}
_inflight: "dict[str, _Job]" = {}


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
