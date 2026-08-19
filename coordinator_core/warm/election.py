"""Windows named-pipe first-instance election.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md, chunk C14.

Exactly one process on the box wins the server pipe. The kernel's
``FILE_FLAG_FIRST_PIPE_INSTANCE`` guarantees at most one ``CreateNamedPipe``
call for a given pipe name creates the first instance; every other caller
fails atomically with ``ERROR_ACCESS_DENIED``. That atomicity IS the
election -- there is no file to clean up on a hard kill and no lease to
expire, which is why this route was chosen over ``CreateMutexW`` (a second
identity that can disagree with the pipe), ``O_CREAT|O_EXCL`` (leaves a file
surviving a hard kill, needing a staleness reaper and force-steal that
``locked_write``'s negative-spec forbids), and ``msvcrt.locking`` on a lock
file (correct, but a second identity, and 199 LOC of ``SingletonLock`` this
plan deliberately does not restore). Route proved live on this box by the
2026-08-14 transport spike (``_winapi.CreateNamedPipe``, a ctypes SDDL
descriptor, asyncio's ``PipeServer``) --
docs/research/spike-verdicts/2026-08-14-stdlib-named-pipe-server-on-windows.md.

Pipe name shape: ``\\\\.\\pipe\\coordinator-core.<user-sid>.<clone-hash>.<engine-token>``.
The namespace is flat and machine-global, so the SID and the resolved clone
path are both load-bearing -- either component missing lets a claude-klabauter clone
and a sibling clone (e.g. klabauter) collide on one server. ``engine_token``
is an opaque generation stamp this module never computes: it exists so a
successor bound to a new token binds immediately while the old generation is
still draining (C17), rather than racing ``FILE_FLAG_FIRST_PIPE_INSTANCE``
against a live instance of the exact same name for the whole drain window.
C16 owns computing the token's value (git-source skew signal); this module
only takes it as a parameter.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Optional

from coordinator_core.warm.engine_root import current_engine_clone

__all__ = [
    "ElectionError",
    "ElectionLost",
    "current_user_sid",
    "pipe_name",
    "elect",
]

ERROR_ACCESS_DENIED = 5


class ElectionError(Exception):
    """Base for election failures raised by this module."""


class ElectionLost(ElectionError):
    """Another process already holds the first instance of this pipe name."""

    def __init__(self, name: str):
        self.pipe_name = name
        super().__init__(f"lost first-instance election for {name!r}")


def _is_windows() -> bool:
    return sys.platform == "win32"


def current_user_sid() -> str:
    """Return the calling process's user SID as an SDDL string (e.g. ``S-1-5-21-...-1002``)."""
    if not _is_windows():
        raise RuntimeError("current_user_sid is Windows-only")
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    token_query = 0x0008
    token_user = 1

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    htoken = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), token_query, ctypes.byref(htoken)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(htoken, token_user, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(htoken, token_user, buf, size.value, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        str_sid = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(str_sid)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return str_sid.value
        finally:
            kernel32.LocalFree(str_sid)
    finally:
        kernel32.CloseHandle(htoken)


def _default_engine_clone() -> Path:
    # Collapsed onto engine_root.current_engine_clone() (plan
    # 2026-08-19-an-engine-root-is-a-stamped-build § C3).
    return current_engine_clone()


def pipe_name(
    engine_token: str,
    *,
    engine_clone: Optional[Path] = None,
    user_sid: Optional[str] = None,
) -> str:
    """Compute this engine clone's server pipe name.

    ``engine_token`` is an opaque generation stamp supplied by the caller
    (C16 computes its value); this function never derives one. ``engine_clone``
    defaults to this repo's resolved root -- pass it explicitly only to name
    a different clone's pipe (e.g. from a shared test helper).
    """
    clone = engine_clone if engine_clone is not None else _default_engine_clone()
    clone_hash = hashlib.sha1(str(Path(clone).resolve()).encode("utf-8")).hexdigest()[:16]
    sid = user_sid if user_sid is not None else current_user_sid()
    return f"\\\\.\\pipe\\coordinator-core.{sid}.{clone_hash}.{engine_token}"


def _build_security_attributes(sid: str):
    import ctypes
    from ctypes import wintypes

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL

    # D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;<sid>) -- no WD, no AN; P blocks inheritance.
    sddl = f"D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;{sid})"
    psd = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(psd), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())

    sa = _SecurityAttributes()
    sa.nLength = ctypes.sizeof(_SecurityAttributes)
    sa.lpSecurityDescriptor = psd
    sa.bInheritHandle = False
    return sa


def elect(name: str, *, user_sid: Optional[str] = None) -> int:
    """Attempt the first-instance election for pipe ``name``.

    Returns the raw pipe handle (an int, matching the private
    ``_winapi``/``asyncio.windows_events`` surface the rest of the transport
    is built on) on a win -- the caller owns it and is responsible for
    wrapping it (e.g. in ``asyncio.windows_events.PipeServer``, per the
    2026-08-14 transport spike) or closing it with ``_winapi.CloseHandle``.

    Raises ``ElectionLost`` when another process already holds the first
    instance (``ERROR_ACCESS_DENIED``) -- the only outcome that means "someone
    else won." Any other ``OSError`` is a real failure and is re-raised
    untouched rather than folded into ``ElectionLost``.
    """
    if not _is_windows():
        raise RuntimeError("elect is Windows-only")
    import ctypes
    import _winapi

    sid = user_sid if user_sid is not None else current_user_sid()
    # security_attributes must outlive the CreateNamedPipe call; kept as a
    # local so it is not collected before the syscall consumes its address.
    security_attributes = _build_security_attributes(sid)

    try:
        return _winapi.CreateNamedPipe(
            name,
            _winapi.PIPE_ACCESS_DUPLEX | _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE,
            _winapi.PIPE_TYPE_MESSAGE | _winapi.PIPE_READMODE_MESSAGE | _winapi.PIPE_WAIT,
            _winapi.PIPE_UNLIMITED_INSTANCES,
            65536,
            65536,
            0,
            ctypes.addressof(security_attributes),
        )
    except OSError as exc:
        if getattr(exc, "winerror", None) == ERROR_ACCESS_DENIED:
            raise ElectionLost(name) from exc
        raise
