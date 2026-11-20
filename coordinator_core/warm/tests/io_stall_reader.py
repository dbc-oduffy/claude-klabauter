"""Bounded, rate-capped synthetic box IO load: N reader threads streaming a tmp file with cache-bypassing reads.

`io_stall_reader(directory)` is a context manager; the file it writes lives under `directory` (a
`tmp_path` in tests) and is removed, with every reader joined, on exit. Bounded by `size_mb` bytes on
disk and `seconds` of reading; stdlib only (ctypes on Windows for unbuffered reads).
"""

from __future__ import annotations

import contextlib
import errno
import mmap
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

CHUNK_BYTES = 4 * 1024 * 1024
DEFAULT_MB_PER_SEC_CAP = 450.0
_BURST_SLACK_SECS = 0.25


class _Pacer:
    """Shared schedule that holds all reader threads together under `mb_per_sec`."""

    def __init__(self, mb_per_sec: float) -> None:
        self._secs_per_chunk = CHUNK_BYTES / (mb_per_sec * 1024 * 1024)
        self._next = time.monotonic()
        self._lock = threading.Lock()

    def wait(self, stop: threading.Event) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(self._next, now - _BURST_SLACK_SECS)
            self._next = slot + self._secs_per_chunk
        delay = slot - now
        if delay > 0:
            stop.wait(delay)


@dataclass
class ReaderStats:
    """Bytes read so far across all reader threads; `mode` names the cache-bypass path in use."""

    mode: str = "buffered"
    bytes_read: int = 0
    errors: list = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, n: int) -> None:
        with self._lock:
            self.bytes_read += n

    def mb_per_sec(self) -> float:
        elapsed = max(time.monotonic() - self.started, 1e-6)
        return self.bytes_read / (1024 * 1024) / elapsed


def _write_file(path: Path, size_mb: int) -> None:
    block = bytes((i * 31 + 7) % 251 for i in range(CHUNK_BYTES))
    with open(path, "wb") as handle:
        for _ in range(max(size_mb * 1024 * 1024 // CHUNK_BYTES, 1)):
            handle.write(block)
        handle.flush()
        os.fsync(handle.fileno())


class _WindowsUnbufferedReader:
    """`CreateFileW(FILE_FLAG_NO_BUFFERING)` + `ReadFile` into a page-aligned buffer."""

    mode = "windows-no-buffering"

    def __init__(self, path: Path) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._k32 = k32
        generic_read, share_all, open_existing, no_buffering, seq_scan = 0x80000000, 7, 3, 0x20000000, 0x08000000
        handle = k32.CreateFileW(str(path), generic_read, share_all, None, open_existing, no_buffering | seq_scan, None)
        if handle in (None, wintypes.HANDLE(-1).value):
            raise OSError(ctypes.get_last_error(), "CreateFileW(NO_BUFFERING) failed")
        self._handle = handle
        self._mm = mmap.mmap(-1, CHUNK_BYTES)
        self._buf = (ctypes.c_char * CHUNK_BYTES).from_buffer(self._mm)

    def read_at(self, offset: int) -> int:
        ctypes, wintypes = self._ctypes, self._wintypes
        if not self._k32.SetFilePointerEx(self._handle, offset, None, 0):
            raise OSError(ctypes.get_last_error(), "SetFilePointerEx failed")
        got = wintypes.DWORD(0)
        if not self._k32.ReadFile(self._handle, self._buf, CHUNK_BYTES, ctypes.byref(got), None):
            raise OSError(ctypes.get_last_error(), "ReadFile failed")
        return got.value

    def close(self) -> None:
        self._buf = None
        self._mm.close()
        self._k32.CloseHandle(self._handle)


class _PosixReader:
    """O_DIRECT (Linux) or F_NOCACHE (macOS) preadv into a page-aligned buffer; else buffered + DONTNEED."""

    def __init__(self, path: Path) -> None:
        self.mode = "buffered"
        self._path_fallback = path
        self._fd = -1
        flags = os.O_RDONLY | getattr(os, "O_DIRECT", 0)
        try:
            self._fd = os.open(path, flags)
            if flags & getattr(os, "O_DIRECT", 0):
                self.mode = "o-direct"
        except OSError:
            self._fd = os.open(path, os.O_RDONLY)
        if self.mode == "buffered" and sys.platform == "darwin":
            import fcntl

            with contextlib.suppress(OSError):
                fcntl.fcntl(self._fd, getattr(fcntl, "F_NOCACHE", 48), 1)
                self.mode = "f-nocache"
        self._mm = mmap.mmap(-1, CHUNK_BYTES)

    def read_at(self, offset: int) -> int:
        try:
            got = os.preadv(self._fd, [self._mm], offset)
        except OSError as exc:
            if exc.errno != errno.EINVAL or self.mode != "o-direct":
                raise
            os.close(self._fd)
            self._fd = os.open(self._path_fallback, os.O_RDONLY)
            self.mode = "buffered"
            got = os.preadv(self._fd, [self._mm], offset)
        if self.mode == "buffered" and hasattr(os, "posix_fadvise"):
            os.posix_fadvise(self._fd, offset, CHUNK_BYTES, os.POSIX_FADV_DONTNEED)
        return got

    def close(self) -> None:
        self._mm.close()
        os.close(self._fd)


def _open_reader(path: Path):
    if sys.platform == "win32":
        try:
            return _WindowsUnbufferedReader(path)
        except OSError:
            return _BufferedReader(path)
    return _PosixReader(path)


class _BufferedReader:
    """Large sequential reads; cached after the first pass, so only a last resort."""

    mode = "buffered"

    def __init__(self, path: Path) -> None:
        self._handle = open(path, "rb", buffering=0)

    def read_at(self, offset: int) -> int:
        self._handle.seek(offset)
        return len(self._handle.read(CHUNK_BYTES))

    def close(self) -> None:
        self._handle.close()


@contextlib.contextmanager
def io_stall_reader(
    directory: Path,
    *,
    threads: int = 4,
    size_mb: int = 512,
    seconds: float = 30.0,
    mb_per_sec: float = DEFAULT_MB_PER_SEC_CAP,
) -> Iterator[ReaderStats]:
    """Load the volume holding `directory` at about `mb_per_sec` for at most `seconds`, reading a `size_mb` file.

    Rate-capped, never uncapped: `mb_per_sec` must be positive (an uncapped NO_BUFFERING reader ran
    9.4-9.6 GB/s and starved the box). Yields `ReaderStats` once a thread has issued its first read.
    Threads stop at `seconds`, or when the block exits, whichever is first; all are joined and the
    file is deleted in `finally`.
    """
    if not mb_per_sec or mb_per_sec <= 0:
        raise ValueError("io_stall_reader is rate-capped: mb_per_sec must be > 0")
    pacer = _Pacer(mb_per_sec)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "io_stall_reader.bin"
    stats = ReaderStats()
    stop = threading.Event()
    primed = threading.Event()
    size = max(size_mb * 1024 * 1024 // CHUNK_BYTES, 1) * CHUNK_BYTES

    def _run(index: int) -> None:
        reader = None
        try:
            reader = _open_reader(path)
            stats.mode = reader.mode
            offset = (index * size // max(threads, 1)) // CHUNK_BYTES * CHUNK_BYTES
            end = time.monotonic() + seconds
            while not stop.is_set() and time.monotonic() < end:
                pacer.wait(stop)
                if stop.is_set():
                    break
                got = reader.read_at(offset)
                stats.add(got)
                primed.set()
                offset += CHUNK_BYTES
                if got < CHUNK_BYTES or offset >= size:
                    offset = 0
        except OSError as exc:
            stats.errors.append(repr(exc))
        finally:
            primed.set()
            if reader is not None:
                reader.close()

    workers = [threading.Thread(target=_run, args=(i,), name=f"io-stall-reader-{i}", daemon=True) for i in range(threads)]
    try:
        _write_file(path, size_mb)
        stats.started = time.monotonic()
        for worker in workers:
            worker.start()
        primed.wait(10.0)
        yield stats
    finally:
        stop.set()
        for worker in workers:
            worker.join(30.0)
        path.unlink(missing_ok=True)
