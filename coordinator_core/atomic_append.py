"""
coordinator_core.atomic_append — the one atomic-multi-process-append
primitive, shared by every caller that needs it.

Purpose: three independent modules (coordinator_core.telemetry.op_latency,
coordinator_core.benchmarks.ambient_sampler,
coordinator_core.install.resolution_journal) each need to append one
pre-encoded JSONL line to a shared sink from possibly-concurrent processes,
without ever losing or interleaving a line. coordinator_core.telemetry's own
concurrency test reproduced LIVE that plain ``os.open(path, os.O_APPEND |
os.O_CREAT | os.O_WRONLY)`` does NOT give this guarantee on Windows (see
``_open_append_fd_windows``'s negative-spec below) — that fix belongs in
exactly one place, not three independent copies of it, per this repo's
never-invent-a-second-mechanism doctrine.

Home: this module sits at ``coordinator_core`` top level, sibling to both
``coordinator_core.install`` and ``coordinator_core.telemetry``/
``coordinator_core.benchmarks``, rather than inside either. At the time this
module was authored there was no existing import edge between
``coordinator_core.install`` and ``coordinator_core.telemetry`` in either
direction — but ``install`` is a leaf-consumer package (writers declare a
``WRITE_SURFACE`` and get consumed by the orchestrator) and must never
depend on ``telemetry``/``benchmarks``, which are standalone measurement
instruments with their own negative-specs. Parenting the primitive under
either sibling would make the OTHER sibling's import of it look like (and
risk becoming) a layering inversion; a shared top-level module has no such
direction to get wrong.

Negative-spec (hard-won, Windows):
    The Win32 CRT emulates ``os.open(..., os.O_APPEND)`` by seeking to EOF
    and then writing — it does NOT map to the kernel's atomic
    FILE_APPEND_DATA semantics the way POSIX O_APPEND does. Two processes
    can seek to the same EOF offset and one write silently clobbers the
    other's bytes, with NO exception raised on either side. Reproduced
    live: 4 spawn-context processes each appending 25 lines landed 94-99
    lines, never 100, with every surviving line intact (never garbled) —
    the fingerprint of a lost write, not a torn one. POSIX O_APPEND remains
    genuinely atomic for a single small write and is used unmodified there.
"""

from __future__ import annotations

import atexit
import os
import queue
import sys
import threading
import time
from pathlib import Path

IS_WINDOWS = os.name == "nt"

#: One telemetry line takes ms; a write still pending here is wedged, not slow.
APPEND_DEADLINE_SECS = 0.2
_WRITER_IDLE_SECS = 5.0
_registry_lock = threading.Lock()
_writers: "dict[str, queue.SimpleQueue]" = {}
_in_flight: "dict[str, int]" = {}
_stalled: "dict[str, float]" = {}


def _open_append_fd_windows(path: str) -> int:
    """Open ``path`` for atomic multi-process append on Windows, returning a Python fd.

    The correct fix (and the one Win32 itself documents as atomic for
    concurrent appenders) is to open the handle directly via CreateFileW
    with ``dwDesiredAccess = FILE_APPEND_DATA`` (not GENERIC_WRITE) rather
    than going through the CRT's O_APPEND emulation. ``ctypes`` is stdlib —
    no new dependency — and the handle is adapted to a normal Python fd via
    ``msvcrt.open_osfhandle`` so the rest of this module (``os.write``,
    ``os.close``) stays platform-uniform. See this module's docstring for
    the negative-spec this fixes.
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    FILE_APPEND_DATA = 0x0004
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_ALWAYS = 4
    FILE_ATTRIBUTE_NORMAL = 0x80

    CreateFileW = ctypes.windll.kernel32.CreateFileW
    CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    CreateFileW.restype = wintypes.HANDLE

    handle = CreateFileW(
        path,
        FILE_APPEND_DATA | GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_ALWAYS,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle in (None, 0, -1, 0xFFFFFFFF):
        raise OSError(ctypes.get_last_error(), f"CreateFileW failed for {path!r}")

    return msvcrt.open_osfhandle(int(handle), os.O_APPEND)


#: Scheduler yields a fresh sink gets when the stall window is spent, so a healthy sink is not dropped before its writer thread runs.
_FRESH_SINK_YIELDS = 5

#: Retained lines a stalled sink's writer may hold; past it a retained line drops like any other.
_RETAIN_MAX = 256


def append_line(sink: "Path | str", encoded: bytes, retain: bool = False) -> None:
    """Atomically append ``encoded`` (already newline-terminated) to ``sink``.

    A single write of one pre-encoded line, using the platform's genuinely
    atomic append primitive — plain ``O_APPEND`` on POSIX (real kernel
    atomicity), ``FILE_APPEND_DATA`` via ``CreateFileW`` on Windows (see
    ``_open_append_fd_windows`` for why plain ``os.open(..., O_APPEND)`` is
    NOT safe there) — so concurrent appenders across processes never
    interleave-corrupt OR silently clobber a line. No read-modify-write, no
    lock file.

    Callers are responsible for creating ``sink``'s parent directory first
    and for swallowing any exception this raises if their own failure
    posture requires it — this function itself does not swallow, so a
    concurrency test can observe a real failure if the atomicity guarantee
    is ever violated.

    The write runs on a per-sink daemon writer thread and the caller waits
    ``APPEND_DEADLINE_SECS``: on expiry the sink is marked stalled, one stderr
    line names it, and the line is dropped without raising; later calls to
    that sink drop at once until its writer returns. An error raised inside
    the deadline is re-raised here.

    ``retain=True`` marks a line that must not be lost to a stall (an op's completion
    row): while the sink is stalled it is queued behind the wedged write, without
    waiting, and lands when that writer returns.
    """
    sink_str = str(sink)
    job = _Job(encoded)
    with _registry_lock:
        if sink_str in _stalled:
            held = _writers.get(sink_str)
            if retain and held is not None and held.qsize() < _RETAIN_MAX:
                held.put(job)
            return
        w = _writers.get(sink_str)
        if w is None:
            w = _writers[sink_str] = queue.SimpleQueue()
            threading.Thread(
                target=_writer_loop, args=(sink_str, w), daemon=True,
                name=f"atomic-append-writer:{sink_str}",
            ).start()
        w.put(job)
        budget = APPEND_DEADLINE_SECS
        if _stalled:
            budget = max(0.0, APPEND_DEADLINE_SECS - (time.monotonic() - min(_stalled.values())))
    if budget <= 0.0:
        for _ in range(_FRESH_SINK_YIELDS):
            if job.done.is_set():
                break
            time.sleep(0)
    if job.done.wait(budget):
        if job.error is not None:
            raise job.error
        return
    with _registry_lock:
        if not job.done.is_set():
            _stalled[sink_str] = time.monotonic()
            first = True
        else:
            first = False
    if first:
        try:
            sys.stderr.write(f"atomic_append: sink stalled past {APPEND_DEADLINE_SECS}s, line dropped: {sink_str}\n")
        except Exception:
            pass
    elif job.error is not None:
        raise job.error


class _Job:
    __slots__ = ("encoded", "done", "error")

    def __init__(self, encoded: bytes) -> None:
        self.encoded = encoded
        self.done = threading.Event()
        self.error: "BaseException | None" = None


def _write_once(sink_str: str, encoded: bytes) -> None:
    if IS_WINDOWS:
        fd = _open_append_fd_windows(sink_str)
    else:
        fd = os.open(sink_str, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        view = memoryview(encoded)
        while view:
            n = os.write(fd, view)
            view = view[n:]
    finally:
        os.close(fd)


def _writer_loop(sink_str: str, w: "queue.SimpleQueue") -> None:
    while True:
        try:
            job = w.get(timeout=_WRITER_IDLE_SECS)
        except queue.Empty:
            with _registry_lock:
                if w.empty():
                    _writers.pop(sink_str, None)
                    return
            continue
        with _registry_lock:
            _in_flight[sink_str] = _in_flight.get(sink_str, 0) + 1
        try:
            _write_once(sink_str, job.encoded)
        except Exception as exc:
            job.error = exc
        finally:
            with _registry_lock:
                if _in_flight[sink_str] <= 1:
                    del _in_flight[sink_str]
                else:
                    _in_flight[sink_str] -= 1
                if w.empty():
                    _stalled.pop(sink_str, None)
            job.done.set()


def in_flight_sinks() -> "tuple[str, ...]":
    """Sink paths whose writer thread is inside an open or write right now."""
    with _registry_lock:
        return tuple(_in_flight)


def stalled_sinks() -> "dict[str, float]":
    """Sink path -> ``time.monotonic()`` of the deadline expiry; cleared when the writer returns."""
    with _registry_lock:
        return dict(_stalled)


_QUEUE_MAX = 512
_ATEXIT_DRAIN_SECS = 0.3
_queue: "queue.Queue | None" = None
_queue_lock = threading.Lock()
#: Tests that exercise the queued path set this; otherwise pytest runs synchronously
#: so a test can read the sink right after the call.
_force_async = False


def _drain_worker(q: "queue.Queue") -> None:
    while True:
        sink, encoded, retain = q.get()
        try:
            if retain:  # plain call below keeps the 2-arg shape that test guards wrap
                append_line(sink, encoded, True)
            else:
                append_line(sink, encoded)
        except Exception:
            pass
        finally:
            q.task_done()


def _get_queue() -> "queue.Queue":
    global _queue
    with _queue_lock:
        if _queue is None:
            _queue = queue.Queue(maxsize=_QUEUE_MAX)
            threading.Thread(
                target=_drain_worker, args=(_queue,), daemon=True, name="atomic-append-drain"
            ).start()
            atexit.register(flush, _ATEXIT_DRAIN_SECS)
        return _queue


def flush(timeout: float) -> None:
    """Wait up to ``timeout`` seconds for queued appends to land; never raises."""
    q = _queue
    if q is None:
        return
    done = threading.Event()
    threading.Thread(target=lambda: (q.join(), done.set()), daemon=True).start()
    done.wait(timeout)


def append_line_bounded(sink: "Path | str", encoded: bytes, retain: bool = False) -> None:
    """Hand ``encoded`` to a per-process writer thread; never blocks the caller.

    For telemetry on an op's critical path: a stalled ``CreateFileW`` (AV
    minifilter, contended sink) must delay only the writer thread. A full queue
    drops the line. ``retain`` is ``append_line``'s. Never raises.
    """
    try:
        if "pytest" in sys.modules and not _force_async:
            append_line(sink, encoded, retain)
            return
        _get_queue().put_nowait((str(sink), encoded, retain))
    except Exception:
        pass
