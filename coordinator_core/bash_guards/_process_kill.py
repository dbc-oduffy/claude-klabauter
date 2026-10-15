"""Spawn-free (pid, ctime)-verified process kill and command-line read.

Implements the KillPrimitives protocol: Windows via ctypes, Linux via /proc and signals, macOS
via sysctl and signals (macOS arm read-not-executed until a Mac session). Never raises: every
failure maps to the documented sentinel (False / None).
"""

from __future__ import annotations

import ctypes
import signal
import sys
import time
from typing import Optional

from coordinator_core.bash_guards import _host_probe as _hp

# Reached through _host_probe: the import-closure pin forbids a direct os import here.
_os = _hp.os

_PROCESS_TERMINATE = 0x1
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x100000
_WAIT_OBJECT_0 = 0x0
_PROCESS_COMMAND_LINE_INFORMATION = 60
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_STATUS_BUFFER_OVERFLOW = 0x80000005
_STATUS_BUFFER_TOO_SMALL = 0xC0000023
_WAIT_BOUND_MS = 3000
_POSIX_TERM_WAIT_S = 1.0
_POSIX_KILL_WAIT_S = 2.0
_POSIX_POLL_S = 0.02

_cache: dict = {}


def _is_windows() -> bool:
    return sys.platform == "win32"


def _kernel():
    """Shared kernel32 plus the TerminateProcess prototype."""
    k = _hp._win()
    if "term" not in _cache:
        from ctypes import wintypes

        k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.TerminateProcess.restype = wintypes.BOOL
        _cache["term"] = True
    return k


def _ntquery():
    if "nt" not in _cache:
        from ctypes import wintypes

        nt = ctypes.WinDLL("ntdll")
        nt.NtQueryInformationProcess.argtypes = [
            wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG),
        ]
        nt.NtQueryInformationProcess.restype = ctypes.c_long
        _cache["nt"] = nt
    return _cache["nt"]


def _posix_gone(pid: int, ctime: int) -> bool:
    """True when (pid, ctime) no longer runs; reaps it first when it is our own child, so a
    zombie does not read alive."""
    try:
        _os.waitpid(pid, _os.WNOHANG)
    except (ChildProcessError, OSError):
        pass
    return not _hp.alive(pid, ctime)


def _posix_terminate_verified(pid: int, ctime: int) -> bool:
    if _hp.creation_time(pid) != ctime:
        return False
    for sig, bound in ((signal.SIGTERM, _POSIX_TERM_WAIT_S), (signal.SIGKILL, _POSIX_KILL_WAIT_S)):
        try:
            _os.kill(pid, sig)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        deadline = time.monotonic() + bound
        while True:
            if _posix_gone(pid, ctime):
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(_POSIX_POLL_S)
    return False


def _posix_command_line(pid: int, ctime: int) -> Optional[str]:
    if _hp.creation_time(pid) != ctime:
        return None
    argv = _hp._darwin_argv(pid) if sys.platform == "darwin" else _hp._linux_argv(pid)
    return " ".join(argv) if argv else None


def terminate_verified(pid: int, ctime: int) -> bool:
    """True only when the process with this (pid, ctime) was terminated and confirmed dead."""
    if not _is_windows():
        if sys.platform == "win32":
            return False
        try:
            return _posix_terminate_verified(pid, ctime)
        except Exception:
            return False
    try:
        k = _kernel()
        h, _err = _hp._win_open(pid, _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE)
        if not h:
            return False
        try:
            if _hp._win_ctime_of(h) != ctime:
                return False
            if not k.TerminateProcess(h, 1):
                return False
            return k.WaitForSingleObject(h, _WAIT_BOUND_MS) == _WAIT_OBJECT_0
        finally:
            k.CloseHandle(h)
    except Exception:
        return False


def command_line(pid: int, ctime: int) -> Optional[str]:
    """Decoded command line of the (pid, ctime)-verified process, or None on any failure."""
    if not _is_windows():
        if sys.platform == "win32":
            return None
        try:
            return _posix_command_line(pid, ctime)
        except Exception:
            return None
    try:
        k = _hp._win()
        h, _err = _hp._win_open(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
        if not h:
            return None
        try:
            if _hp._win_ctime_of(h) != ctime:
                return None
            return _read_command_line(h)
        finally:
            k.CloseHandle(h)
    except Exception:
        return None


def _read_command_line(handle) -> Optional[str]:
    from ctypes import wintypes

    nt = _ntquery()
    need = wintypes.ULONG(0)
    nt.NtQueryInformationProcess(handle, _PROCESS_COMMAND_LINE_INFORMATION, None, 0, ctypes.byref(need))
    size = need.value
    if size < 16 or size > 1 << 20:
        return None
    for _ in range(3):
        buf = ctypes.create_string_buffer(size)
        status = nt.NtQueryInformationProcess(
            handle, _PROCESS_COMMAND_LINE_INFORMATION, buf, size, ctypes.byref(need)
        ) & 0xFFFFFFFF
        if status == 0:
            # UNICODE_STRING { USHORT Length; USHORT MaximumLength; PWSTR Buffer } with the
            # characters stored inline after the header; Buffer points into buf.
            length = int.from_bytes(buf.raw[0:2], "little")
            ptr = int.from_bytes(buf.raw[8:16], "little") if ctypes.sizeof(ctypes.c_void_p) == 8 \
                else int.from_bytes(buf.raw[4:8], "little")
            if length == 0 or ptr == 0:
                return None
            return ctypes.wstring_at(ptr, length // 2)
        if status in (_STATUS_INFO_LENGTH_MISMATCH, _STATUS_BUFFER_OVERFLOW, _STATUS_BUFFER_TOO_SMALL):
            size = need.value
            if size < 16 or size > 1 << 20:
                return None
            continue
        return None
    return None


__all__ = ["terminate_verified", "command_line"]
