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

import os
from pathlib import Path

# os.name, not platform.system(): the latter's first call costs ~28ms on Windows
# (measured 2026-08-08) because it resolves the full uname/win32_ver triple, and
# this module sits on the engine's per-invocation import path where that lands on
# every one of the ~85ms-floor invocations. os.name is a preset constant.
IS_WINDOWS = os.name == "nt"


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


def append_line(sink: "Path | str", encoded: bytes) -> None:
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
    """
    sink_str = str(sink)
    if IS_WINDOWS:
        fd = _open_append_fd_windows(sink_str)
    else:
        fd = os.open(sink_str, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        # os.write() is not guaranteed to write the full buffer in one call
        # (short writes are POSIX-legal); loop until all bytes land so a
        # short write can never surface as a partial/corrupted line under
        # contention. Each individual os.write() call remains the atomic
        # unit — this loop only guards against having to make more than one.
        view = memoryview(encoded)
        while view:
            n = os.write(fd, view)
            view = view[n:]
    finally:
        os.close(fd)
