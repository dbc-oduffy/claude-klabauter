"""Warm-engine endpoint election -- Windows named pipe, POSIX unix socket.

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

THE POSIX BRANCH (added 2026-08-21) -- ``socket_path`` / ``elect_unix_socket``
below, beside the Windows pair rather than replacing it. Every Windows symbol
above is untouched: ``elect`` still refuses to run off Windows and
``elect_unix_socket`` still refuses to run on it, so there is no platform on
which the two can be confused for one another.

POSIX has no ``FILE_FLAG_FIRST_PIPE_INSTANCE``, and that single missing
guarantee is the whole reason the POSIX half is longer than the Windows half.
``bind()`` on a unix socket fails ``EADDRINUSE`` when the path merely EXISTS --
whether or not anything is listening on it -- so a hard-killed server leaves a
socket file behind that makes every future ``bind()`` fail against nothing.
That is EXACTLY the stale-artifact problem the docstring above records as the
reason ``O_CREAT|O_EXCL`` was rejected on Windows, re-entered here because
POSIX offers no alternative that avoids it. The port therefore cannot be a
transliteration of ``elect``; it needs two mechanisms Windows gets for free:

  1. ``reclaim_stale_socket`` -- connect-probe-then-unlink. Connect to the
     existing path: ``ECONNREFUSED`` proves nothing is listening, so the file
     is a corpse and is unlinked; a successful connect (or a connect that
     times out, or fails any OTHER way) proves or implies a live owner and the
     election is LOST. The file is never removed on doubt -- an unlink-on-
     unknown would be the "force-steal" the Windows docstring rules out, with
     a live peer's endpoint as the thing stolen.
  2. ``_acquire_election_lock`` -- an ``flock`` held across probe/unlink/bind.
     Without it, two servers can both see the same corpse, both unlink, and
     the second unlink deletes the FIRST one's freshly-bound live socket,
     leaving a healthy server bound to an unlinked inode that no client can
     ever reach. ``flock`` is the one exclusion primitive whose hold is
     released by the KERNEL when the holder dies, so unlike a lock FILE it
     needs no staleness reaper of its own -- the lock file persists, but it is
     never consulted for liveness, only locked.

SOCKET PATH IS A TWO-IMPLEMENTATION CONTRACT, PM-LOCKED 2026-08-21. The
shape is ``<base>/coordinator/warm/<clone-hash>/<engine-token>.sock``, i.e.
``breadcrumb.svc_dir()`` plus the token -- the SAME per-user, per-clone
runtime directory the breadcrumb already resolves, reused rather than given a
second derivation. ``<base>`` is exactly ``breadcrumb.runtime_base()``:
``$COORDINATOR_WARM_RUNTIME_BASE``, else ``%LOCALAPPDATA%``, else
``~/.cache``. There is deliberately NO ``$XDG_RUNTIME_DIR`` branch, and that
is a contract rather than a preference -- see ``breadcrumb._runtime_base``'s
own docstring for why. The C door (``warm/door/``) recomputes this path
independently to find the server, and a path the binder and the door disagree
about raises NO ERROR ANYWHERE: the door finds nothing, falls through to cold
dispatch forever, and every surface stays green while the warm engine is
unreachable. ``socket_path`` below is the production helper; anything that
needs this path calls it rather than deriving its own.

The SID's job in the pipe name (per-user isolation of a machine-global flat
namespace) is done on POSIX by that directory being user-local and by
``ensure_private_dir`` verifying, AFTER creation, that the leaf is mode 0700
and owned by ``getuid()`` -- umask masks a requested ``mkdir`` mode, so
requesting 0700 proves nothing. The interposed ``coordinator/`` and
``coordinator/warm/`` are verified too (``_verify_owned_ancestor``): a 0700
leaf under a parent another account can write is a directory that account can
rename aside and substitute, and every check on "the leaf" would then pass on
theirs. The CONTAINING DIRECTORY is the security boundary, not the socket
file's own mode: Linux enforces the socket's mode bits on connect, macOS/BSD
do not reliably, so the socket's own ``chmod 0600`` below is defence in depth
and never the thing relied on.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Callable, Optional

from coordinator_core.warm.engine_root import current_engine_clone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import threading
    from socket import socket

__all__ = [
    "ElectionError",
    "ElectionLost",
    "InsecureRuntimeDirError",
    "SocketPathTooLongError",
    "current_user_sid",
    "sandbox_principal_sids",
    "pipe_name",
    "elect",
    "SOCKET_SUFFIX",
    "UNIX_LISTEN_BACKLOG",
    "STALE_PROBE_TIMEOUT_SECS",
    "PROBE_LIVE",
    "PROBE_STALE",
    "PROBE_ABSENT",
    "current_user_id",
    "socket_path",
    "ensure_private_dir",
    "probe_endpoint",
    "reclaim_stale_socket",
    "elect_unix_socket",
    "elect_exclusive_lock",
    "release_exclusive_lock",
    "install_predecessor",
    "retire_predecessor",
    "retire_superseded_server",
    "RETIRE_TERMINATED",
    "RETIRE_NOT_VERIFIED",
    "socket_identity",
    "unlink_if_owned",
]

ERROR_ACCESS_DENIED = 5


class ElectionError(Exception):
    pass


class ElectionLost(ElectionError):

    def __init__(self, name: str) -> None:
        self.pipe_name = name
        super().__init__(f"lost first-instance election for {name!r}")

    @property
    def endpoint(self) -> str:
        return self.pipe_name


class InsecureRuntimeDirError(ElectionError):
    pass


class SocketPathTooLongError(ElectionError):
    pass


#: BYTE read mode for both named-pipe creation sites -- THIS one (the first
#: instance) and `server._create_pipe_instance` (every follow-on one), which
#: imports this name rather than repeating the value. They must agree: if they
#: did not, whether a request is served would depend on which instance
#: happened to accept it. Defined here rather than in `server` because
#: `server` already imports this module and the reverse edge would be a cycle.
#:
#: `_winapi` publishes no `PIPE_READMODE_BYTE` because the flag IS zero; the
#: constant exists so both sites NAME the choice instead of silently omitting
#: a flag.
#:
#: WHY BYTE AND NOT MESSAGE. The wire protocol is one newline-terminated JSON
#: line in each direction -- `server._handle_connection` reads it with
#: `io.readline()`, `door.c` scans for a newline byte. That is a byte-stream
#: protocol; the pipe's message framing was read by nothing, and one leg of
#: it was actively fatal.
#:
#: THE DEFECT THIS CLOSES (2026-09-06, reported from a live plan-blitz wave).
#: Under `PIPE_READMODE_MESSAGE` a `ReadFile` whose buffer is smaller than the
#: pending message fails with `ERROR_MORE_DATA` instead of returning a partial
#: read. `server._wrap_handle` hands the pipe to a `BufferedReader` whose
#: underlying reads are `io.DEFAULT_BUFFER_SIZE` (8192) -- so EVERY request
#: frame over 8192 bytes made `io.readline()` raise `OSError`, which
#: `_handle_connection` catches with a bare `return`, closing the connection
#: without a reply. The caller's door had already delivered the bytes, so it
#: could only emit `-32004 warm dispatch indeterminate`: the worst failure
#: shape this transport has, on a request the server never even parsed.
#: Measured threshold: 8192 bytes served, 8193 refused, exactly.
#:
#: THE ASYMMETRY THAT HID IT. A CLIENT handle opened with `CreateFile`
#: (`client._open_pipe`'s `open(endpoint, "r+b")`, and `door.c`'s
#: `CreateFileW`) defaults to BYTE read mode regardless of the pipe's type,
#: and neither ever calls `SetNamedPipeHandleState`. Large RESPONSES therefore
#: always worked and only large REQUESTS died -- which reads as "that one op
#: is broken" rather than "every op with a big payload is".
#:
#: `PIPE_TYPE_MESSAGE` is deliberately LEFT at both sites: it governs how a
#: handle's WRITES are framed, which no reader on either end depends on, so
#: changing it would widen this fix's blast radius for nothing.
_PIPE_READMODE_BYTE = 0x00000000

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
    return current_engine_clone()


def pipe_name(
    engine_token: str,
    *,
    engine_clone: Optional[Path] = None,
    user_sid: Optional[str] = None,
) -> str:
    clone = engine_clone if engine_clone is not None else _default_engine_clone()
    clone_hash = hashlib.sha1(str(Path(clone).resolve()).encode("utf-8")).hexdigest()[:16]
    sid = user_sid if user_sid is not None else current_user_sid()
    return f"\\\\.\\pipe\\coordinator-core.{sid}.{clone_hash}.{engine_token}"


_SID_TEXT = re.compile(r"^S-1-[0-9-]+$")

SANDBOX_PRINCIPALS_KEY = "warm.sandbox_principals"
DEFAULT_SANDBOX_PRINCIPALS = ("CodexSandboxUsers",)


def _configured_sandbox_principals() -> tuple:
    from coordinator_core.machine_resolver import registry_get

    raw = registry_get(SANDBOX_PRINCIPALS_KEY)
    if raw is None:
        return DEFAULT_SANDBOX_PRINCIPALS
    names = tuple(n.strip() for n in str(raw).replace(";", ",").split(",") if n.strip())
    return names or DEFAULT_SANDBOX_PRINCIPALS


def _lookup_account_sid(name: str) -> Optional[str]:
    """SID string for account ``name`` via ``LookupAccountNameW``, or ``None``
    when it does not resolve on this machine."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.LookupAccountNameW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.LookupAccountNameW.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    sid_size = wintypes.DWORD(0)
    dom_size = wintypes.DWORD(0)
    use = wintypes.DWORD(0)
    advapi32.LookupAccountNameW(None, name, None, ctypes.byref(sid_size), None, ctypes.byref(dom_size), ctypes.byref(use))
    if not sid_size.value:
        return None
    sid_buf = ctypes.create_string_buffer(sid_size.value)
    dom_buf = ctypes.create_unicode_buffer(max(dom_size.value, 1))
    if not advapi32.LookupAccountNameW(
        None, name, sid_buf, ctypes.byref(sid_size), dom_buf, ctypes.byref(dom_size), ctypes.byref(use)
    ):
        return None
    str_sid = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(sid_buf, ctypes.byref(str_sid)):
        return None
    try:
        return str_sid.value
    finally:
        kernel32.LocalFree(str_sid)


@functools.lru_cache(maxsize=1)
def sandbox_principal_sids() -> tuple:
    """SIDs of the sandbox principals a pipe DACL admits beyond the owner:
    settings key ``warm.sandbox_principals`` (comma-separated account names),
    default ``CodexSandboxUsers``, each resolved in-process. An unresolvable
    name contributes nothing. Windows-only; empty elsewhere. Cached."""
    if not _is_windows():
        return ()
    sids = []
    for name in _configured_sandbox_principals():
        try:
            resolved = _lookup_account_sid(name)
        except OSError:
            resolved = None
        if resolved and _SID_TEXT.match(resolved) and resolved not in sids:
            sids.append(resolved)
    return tuple(sids)


@functools.lru_cache(maxsize=1)
def current_logon_sid() -> Optional[str]:
    """The calling process's logon-session SID (``S-1-5-5-X-Y``), or ``None``
    when the token carries none or cannot be read. A restricted token (the codex
    workspace-write sandbox) lists it among its restricting SIDs."""
    if not _is_windows():
        return None
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    token_query = 0x0008
    token_groups = 2
    se_group_logon_id = 0xC0000000

    class _SID_AND_ATTRIBUTES(ctypes.Structure):
        # Trap: never name this field `the Game Dev Reviewer` -- the publish persona substitute rewrites
        # that token to prose and the klabauter copy stops parsing (71b3d8).
        _fields_ = [("psid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

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
        return None
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(htoken, token_groups, None, 0, ctypes.byref(size))
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(htoken, token_groups, buf, size.value, ctypes.byref(size)):
            return None
        count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
        offset = ctypes.sizeof(ctypes.c_void_p)
        groups = (_SID_AND_ATTRIBUTES * count).from_buffer(buf, offset)
        for group in groups:
            if (group.Attributes & se_group_logon_id) != se_group_logon_id:
                continue
            str_sid = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(group.psid, ctypes.byref(str_sid)):
                return None
            try:
                return str_sid.value
            finally:
                kernel32.LocalFree(str_sid)
        return None
    finally:
        kernel32.CloseHandle(htoken)


def _pipe_sddl(sid: str, logon_sid: Optional[str] = None) -> str:
    """The warm pipe DACL. A restricted token must pass the DACL for its user
    SID and again for a restricting SID, so the server's own logon SID gets
    read/write: the pipe stays scoped to this logon session. No Everyone ACE."""
    sddl = f"D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;{sid})"
    if logon_sid:
        sddl += f"(A;;FRFW;;;{logon_sid})"
    return sddl


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

    sddl = _pipe_sddl(sid, current_logon_sid())
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
    security_attributes = _build_security_attributes(sid)

    try:
        handle = _winapi.CreateNamedPipe(
            name,
            _winapi.PIPE_ACCESS_DUPLEX | _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE,
            _winapi.PIPE_TYPE_MESSAGE | _PIPE_READMODE_BYTE | _winapi.PIPE_WAIT,
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
    _retire_predecessor_if_warm_server()
    return handle


#: The election's per-install gap. The endpoint name embeds the engine token,
#: so ``FILE_FLAG_FIRST_PIPE_INSTANCE`` / ``bind`` only exclude a second server
#: of the SAME generation: a successor with a new token wins its own endpoint
#: while a predecessor that never drained (its idle watchdog starved, or the
#: root is unstamped so ``token_stale`` is always False) keeps serving, and two
#: servers answer for one install. The breadcrumb is the install's single
#: current-generation record, so a winner reads the predecessor off it BEFORE
#: overwriting it and retires that pid after a drain-ceiling grace.
RETIRE_GRACE_MARGIN_SECS = 5.0
RETIRE_TERMINATED = "terminated"
RETIRE_NOT_VERIFIED = "not-verified"
_START_EPOCH_TOLERANCE_SECS = 1


def _running_as_warm_server() -> bool:
    """True only in the process whose ``__main__`` is ``warm/server.py``: the
    retirement must never fire from a test, a client or the supervisor that
    merely calls ``elect``."""
    main_file = getattr(sys.modules.get("__main__"), "__file__", None)
    if not main_file:
        return False
    path = Path(main_file)
    return path.name == "server.py" and path.parent.name == "warm"


def install_predecessor(
    engine_clone: Optional[Path] = None, *, self_pid: Optional[int] = None
) -> Optional[tuple]:
    """``(pid, start_epoch)`` the install's breadcrumb names as the current server,
    or ``None`` when it names nothing verifiable or names ``self_pid``. A record
    without a positive start epoch is never returned: a pid alone is not an identity."""
    from coordinator_core.warm.breadcrumb import read_breadcrumb

    record = read_breadcrumb(engine_clone)
    if not record:
        return None
    pid = record.get("pid")
    epoch = record.get("stable_pid_start_epoch")
    for value in (pid, epoch):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            return None
    if pid == (self_pid if self_pid is not None else os.getpid()):
        return None
    return pid, epoch


def _process_start_epoch(pid: int) -> Optional[int]:
    from coordinator_core.session.core import _win_create_time_epoch

    try:
        return _win_create_time_epoch(pid)
    except Exception:  # noqa: BLE001 -- an unreadable identity is never a kill verdict
        return None


def _terminate_process_tree(pid: int) -> None:
    import psutil

    try:
        root = psutil.Process(pid)
        victims = root.children(recursive=True) + [root]
    except psutil.Error:
        return
    for proc in victims:
        try:
            proc.terminate()
        except psutil.Error:
            pass
    psutil.wait_procs(victims, timeout=2.0)
    for proc in victims:
        try:
            if proc.is_running():
                proc.kill()
        except psutil.Error:
            pass


def retire_predecessor(
    pid: int,
    start_epoch: int,
    *,
    grace_secs: float,
    start_epoch_of: Callable[[int], Optional[int]] = _process_start_epoch,
    terminate: Callable[[int], None] = _terminate_process_tree,
    sleep: Optional[Callable[[float], None]] = None,
) -> str:
    """After ``grace_secs`` (the predecessor's own drain window), terminate
    ``pid`` and its workers iff it is still the process born at ``start_epoch``.
    A dead, reused or unreadable pid is left alone (``RETIRE_NOT_VERIFIED``)."""
    import time as _time

    (sleep or _time.sleep)(grace_secs)
    current = start_epoch_of(pid)
    if current is None or abs(current - start_epoch) > _START_EPOCH_TOLERANCE_SECS:
        return RETIRE_NOT_VERIFIED
    terminate(pid)
    return RETIRE_TERMINATED


def retire_superseded_server(
    engine_clone: Optional[Path] = None, *, grace_secs: Optional[float] = None
) -> Optional["threading.Thread"]:
    """Read the install's predecessor off the breadcrumb (call BEFORE the winner
    rewrites it) and retire it on a daemon thread after the drain grace. Returns
    the thread, or ``None`` when there is no predecessor. Never raises."""
    import threading

    try:
        prior = install_predecessor(engine_clone)
        if prior is None:
            return None
        if grace_secs is None:
            from coordinator_core.warm.lifecycle import _drain_ceiling_secs

            grace_secs = _drain_ceiling_secs() + RETIRE_GRACE_MARGIN_SECS
        pid, epoch = prior
        print(
            f"[warm-server] retiring predecessor pid {pid} after {grace_secs:.0f}s: "
            "one warm server per install",
            file=sys.stderr,
        )
        thread = threading.Thread(
            target=retire_predecessor,
            args=(pid, epoch),
            kwargs={"grace_secs": grace_secs},
            name="warm-retire-predecessor",
            daemon=True,
        )
        thread.start()
        return thread
    except Exception as exc:  # noqa: BLE001 -- retirement never blocks the winner's boot
        print(f"[warm-server] predecessor retirement not armed: {exc!r}", file=sys.stderr)
        return None


def _retire_predecessor_if_warm_server() -> None:
    if _running_as_warm_server():
        retire_superseded_server()


SOCKET_SUFFIX = ".sock"

LOCK_SUFFIX = ".lock"

#: `listen()` backlog. Deliberately larger than the server's acceptor-thread
#: count: the backlog is what absorbs a connection burst in the window between
#: one `accept()` returning and the next being posted, which on Windows is
#: covered instead by the pre-created pending pipe-instance pool
#: (`server.PENDING_LISTENER_POOL_SIZE`). A client that overflows it sees
#: ECONNREFUSED, which `warm.client`'s anti-storm table already handles as a
#: cold outcome -- but sizing it to the acceptor pool would guarantee that
#: outcome under exactly the load this engine is built for.
UNIX_LISTEN_BACKLOG = 128

#: Connect-timeout for the staleness probe. A live server answers a unix-socket
#: connect in microseconds (it is a memory operation, no network stack), and a
#: dead one answers ECONNREFUSED just as fast. Only a live-but-backlog-full
#: server pays this, and that outcome is read as LIVE, so the timeout bounds a
#: case whose verdict does not depend on waiting it out.
STALE_PROBE_TIMEOUT_SECS = 0.25

PROBE_LIVE = "live"
PROBE_STALE = "stale"
PROBE_ABSENT = "absent"

SUN_PATH_MAX_BYTES = 100


def _is_posix() -> bool:
    return not _is_windows()


def current_user_id() -> str:
    if not _is_posix():
        raise RuntimeError("current_user_id is POSIX-only")
    return str(os.getuid())


def socket_path(
    engine_token: str,
    *,
    engine_clone: Optional[Path] = None,
) -> Path:
    """Compute this engine clone's server socket path.

    ``<svc dir>/<engine-token>.sock``. ``svc_dir`` is imported function-locally
    from ``warm.breadcrumb`` rather than at module scope so this module keeps
    its current import graph on the Windows path, where nothing calls this.

    Raises ``SocketPathTooLongError`` when the result will not fit
    ``sockaddr_un.sun_path`` -- checked here rather than left to ``bind()``,
    which reports it as an unexplained ``OSError``. The realistic trigger is a
    long ``XDG_RUNTIME_DIR`` or ``COORDINATOR_WARM_RUNTIME_BASE``, not a long
    token: the token is 16 hex characters and the clone hash 16 more.
    """
    from coordinator_core.warm.breadcrumb import svc_dir

    path = svc_dir(engine_clone) / f"{engine_token}{SOCKET_SUFFIX}"
    encoded = len(os.fsencode(str(path)))
    if encoded > SUN_PATH_MAX_BYTES:
        raise SocketPathTooLongError(
            f"socket path is {encoded} bytes, over the {SUN_PATH_MAX_BYTES}-byte "
            f"sun_path budget: {str(path)!r}"
        )
    return path


def _interposed_ancestors(path: Path, base: Optional[Path]) -> list:
    if base is None:
        return []
    base = Path(base)
    guarded = []
    for ancestor in Path(path).parents:
        if ancestor == base or base not in ancestor.parents:
            break
        guarded.append(ancestor)
    return guarded


def _verify_owned_ancestor(path: Path) -> None:
    """Verify one INTERPOSED directory: ours, a real directory, and not
    writable by group or other.

    A 0700 leaf proves nothing about a parent someone else can write. Given a
    writable ``<base>/coordinator/warm``, another local account renames our
    per-clone directory aside and substitutes its own -- and every check
    ``ensure_private_dir`` then performs on "the leaf" passes, on the
    attacker's directory. This closes that substitution vector. It is the one
    hardening the C door added beyond the original design; both halves check
    the same thing on purpose.

    A WEAKER TEST THAN THE LEAF'S, DELIBERATELY. The leaf must be 0700
    because it is the connect boundary. An interposed directory only has to
    be unwritable by anyone else -- a group- or world-READABLE
    ``~/.cache/coordinator`` leaks nothing, since the leaf inside it refuses
    traversal, and demanding 0700 here would reject a directory an earlier
    version of this code legitimately created at 0755.
    """
    import stat as _stat

    st = os.lstat(path)
    if _stat.S_ISLNK(st.st_mode):
        raise InsecureRuntimeDirError(f"runtime ancestor is a symlink: {str(path)!r}")
    if not _stat.S_ISDIR(st.st_mode):
        raise InsecureRuntimeDirError(f"runtime ancestor is not a directory: {str(path)!r}")
    if st.st_uid != os.getuid():
        raise InsecureRuntimeDirError(
            f"runtime ancestor is owned by uid {st.st_uid}, not {os.getuid()}: {str(path)!r}"
        )
    if st.st_mode & 0o022:
        os.chmod(path, _stat.S_IMODE(st.st_mode) & ~0o022)
        st = os.lstat(path)
        if st.st_mode & 0o022:
            raise InsecureRuntimeDirError(
                f"runtime ancestor is group/other-writable "
                f"({_stat.S_IMODE(st.st_mode):04o}) after chmod: {str(path)!r}"
            )


def ensure_private_dir(path: Path, *, base: Optional[Path] = None) -> Path:
    import stat as _stat

    path = Path(path)
    os.makedirs(path, mode=0o700, exist_ok=True)

    for ancestor in _interposed_ancestors(path, base):
        _verify_owned_ancestor(ancestor)

    st = os.lstat(path)
    if _stat.S_ISLNK(st.st_mode):
        raise InsecureRuntimeDirError(f"runtime directory is a symlink: {str(path)!r}")
    if not _stat.S_ISDIR(st.st_mode):
        raise InsecureRuntimeDirError(f"runtime path is not a directory: {str(path)!r}")
    if st.st_uid != os.getuid():
        raise InsecureRuntimeDirError(
            f"runtime directory is owned by uid {st.st_uid}, not {os.getuid()}: {str(path)!r}"
        )
    if (st.st_mode & 0o077) or (st.st_mode & 0o700) != 0o700:
        os.chmod(path, 0o700)
        st = os.lstat(path)
        if (st.st_mode & 0o077) or (st.st_mode & 0o700) != 0o700:
            raise InsecureRuntimeDirError(
                f"runtime directory is mode {_stat.S_IMODE(st.st_mode):04o} after chmod, "
                f"not 0700: {str(path)!r}"
            )
    return path


def probe_endpoint(path: Path, *, timeout: float = STALE_PROBE_TIMEOUT_SECS) -> str:
    """Connect to ``path`` and immediately disconnect. Returns one of
    ``PROBE_LIVE`` / ``PROBE_STALE`` / ``PROBE_ABSENT``.

    ``ECONNREFUSED`` on a unix socket means the path exists but no process
    holds a listening socket bound to it -- the ONLY proof of staleness POSIX
    offers, and the reason this probe exists at all.

    A CONNECT IS NOT FREE, unlike the Windows ``WaitNamedPipeW`` this is the
    analog of (which asks the kernel about the pipe and connects to nothing).
    A live server accepts this connection, enqueues it, reads EOF, and drops
    it -- one no-op round through its accept/queue/worker path per probe. That
    cost is accepted because the alternative is not probing, and not probing
    means either never reclaiming a stale socket or unlinking one on suspicion.

    A connect that TIMES OUT reads LIVE, not stale: a listening socket whose
    backlog is full is exactly the busy-server case, and it is the one outcome
    where guessing wrong unlinks a healthy peer's endpoint. Every other
    ``OSError`` (``EACCES``, ``EPERM``, anything unanticipated) propagates
    rather than being folded into a verdict -- see ``reclaim_stale_socket``.
    """
    import socket as _socket

    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
    except ConnectionRefusedError:
        return PROBE_STALE
    except FileNotFoundError:
        return PROBE_ABSENT
    except (TimeoutError, _socket.timeout):
        return PROBE_LIVE
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return PROBE_LIVE


def reclaim_stale_socket(
    path: Path,
    *,
    probe: Callable[[Path], str] = probe_endpoint,
) -> bool:
    verdict = probe(path)
    if verdict != PROBE_STALE:
        return False
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    return True


def _acquire_election_lock(path: Path) -> int:
    """Take the non-blocking exclusive ``flock`` guarding this endpoint's
    probe/unlink/bind sequence. Returns an fd the caller must release.

    NON-BLOCKING BY DESIGN. A contended lock means another process is inside
    its own election for this exact endpoint right now, and the answer this
    process needs is available immediately: it has lost. Waiting for the lock
    would buy nothing -- the winner's socket will be bound by the time any
    wait returned -- and would put an unbounded wait on a process the load
    norm caps in the sub-second range.

    Held by the KERNEL, so a holder that is hard-killed releases it. The lock
    FILE survives, which is harmless precisely because it is never read: its
    existence is not evidence of anything, unlike the ``O_CREAT|O_EXCL`` lock
    file the module docstring rejects.
    """
    import errno as _errno
    import fcntl as _fcntl

    lock_path = Path(str(path) + LOCK_SUFFIX)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (_errno.EWOULDBLOCK, _errno.EAGAIN, _errno.EACCES):
            raise ElectionLost(str(path)) from exc
        raise
    return fd


def elect_exclusive_lock(path: Path) -> int:
    """POSIX counterpart of ``elect`` for a caller whose election is a pure
    LOCK and never a transport.

    ``elect`` wins a named-pipe FIRST INSTANCE, which is simultaneously the
    Windows exclusion primitive and the endpoint clients dial. A caller that
    only needs the exclusion half -- ``warm.supervisor``, whose own transport
    is a TCP port it binds separately -- has no socket to elect on POSIX and
    must not invent one just to hold a lock. This is that exclusion half on
    its own: the same ``flock`` ``elect_unix_socket`` takes around its
    bind sequence, taken over ``<path>.lock`` and handed back to be held for
    as long as the caller means to exclude its peers.

    Raises ``ElectionLost`` when another process holds it -- the same one
    outcome that means "someone else won" in both other election arms, so a
    caller's ``except election.ElectionLost`` covers every platform.
    Released by ``release_exclusive_lock``, and by the kernel on process
    death regardless.

    ``ensure_private_dir`` runs first, exactly as in ``elect_unix_socket``
    and for both of its reasons: ``_acquire_election_lock`` opens a sidecar
    IN that directory, so the directory must exist before the lock can be
    taken at all, and the directory is the connect-permission boundary this
    package verifies rather than requests.
    """
    if not _is_posix():
        raise RuntimeError("elect_exclusive_lock is POSIX-only")

    from coordinator_core.warm.breadcrumb import runtime_base

    path = Path(path)
    ensure_private_dir(path.parent, base=runtime_base())
    return _acquire_election_lock(path)


def release_exclusive_lock(fd) -> None:
    _release_election_lock(fd)


def _release_election_lock(fd) -> None:
    if fd is None:
        return
    try:
        import fcntl as _fcntl

        _fcntl.flock(fd, _fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def socket_identity(path: Path) -> Optional[tuple]:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def unlink_if_owned(path: Path, identity: Optional[tuple]) -> bool:
    if identity is None:
        return False
    if socket_identity(path) != identity:
        return False
    try:
        os.unlink(path)
    except OSError:
        return False
    return True


def elect_unix_socket(
    path: Path,
    *,
    backlog: int = UNIX_LISTEN_BACKLOG,
    probe: Callable[[Path], str] = probe_endpoint,
    use_lock: bool = True,
) -> socket:
    """Attempt the election for unix socket ``path``. POSIX counterpart of
    ``elect``.

    Returns a bound, LISTENING ``socket.socket`` on a win -- the caller owns it
    and is responsible for closing it and for unlinking the path
    (``unlink_if_owned``). Raises ``ElectionLost`` when a live server already
    owns this endpoint, or when another process holds the election lock. Any
    other ``OSError`` is a real failure and propagates untouched, matching
    ``elect``'s own "only one outcome means someone else won" contract.

    Sequence, and why each step is there:

      1. ``ensure_private_dir`` on the containing directory -- the connect
         boundary (module docstring), verified rather than requested, and
         with ``base`` supplied so ``coordinator/`` and ``coordinator/warm/``
         are verified as ours too. A 0700 leaf under a parent someone else
         can write is a directory they can rename aside and replace.

         OUTSIDE THE LOCK, NECESSARILY -- not an oversight, and asked about
         once in review already. ``_acquire_election_lock`` opens
         ``<path><LOCK_SUFFIX>``, which lives IN this directory, so the
         directory has to exist before the lock can be taken at all. The
         step is safe outside because it is idempotent (``exist_ok=True``,
         and a chmod to a mode already set) and because it VERIFIES rather
         than trusts: two peers racing it both arrive at the same checked
         state, and a peer that loses the lock a moment later has changed
         nothing. Note what this means for the guarantee below: the lock
         makes steps 3-5 atomic, NOT steps 1-5, and it is not claimed to.
      2. ``_acquire_election_lock`` -- makes steps 3-5 atomic against a peer
         running the same sequence, which is what stops two servers from each
         unlinking the other's freshly-bound socket.
      3. ``bind``. Success ends it: nothing was in the way.
      4. On ``EADDRINUSE`` only, ``reclaim_stale_socket``. A live owner or an
         unreadable verdict ends it as ``ElectionLost``; a proven corpse is
         unlinked.
      5. Re-bind, ONCE. A second ``EADDRINUSE`` is not retried in a loop -- a
         path that reappears while this process holds the election lock is a
         condition retrying cannot resolve, and an unbounded reclaim loop is
         the force-steal the Windows docstring forbids wearing a retry's
         clothes.

    ``use_lock=False`` exists for the same reason ``probe`` is injectable: to
    let a test drive the bind/reclaim/re-bind machine on its own. It is not an
    operator knob, and no code path in this package passes it.
    """
    import errno as _errno
    import socket as _socket

    if not _is_posix():
        raise RuntimeError("elect_unix_socket is POSIX-only")

    from coordinator_core.warm.breadcrumb import runtime_base

    path = Path(path)
    ensure_private_dir(path.parent, base=runtime_base())

    lock_fd = _acquire_election_lock(path) if use_lock else None
    try:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        bound_identity = None
        try:
            try:
                sock.bind(str(path))
            except OSError as exc:
                if exc.errno != _errno.EADDRINUSE:
                    raise
                if not reclaim_stale_socket(path, probe=probe):
                    raise ElectionLost(str(path)) from exc
                try:
                    sock.bind(str(path))
                except OSError as exc2:
                    if exc2.errno == _errno.EADDRINUSE:
                        raise ElectionLost(str(path)) from exc2
                    raise
            bound_identity = socket_identity(path)

            os.chmod(path, 0o600)
            sock.listen(backlog)
        except BaseException:
            try:
                sock.close()
            except OSError:
                pass
            unlink_if_owned(path, bound_identity)
            raise
        _retire_predecessor_if_warm_server()
        return sock
    finally:
        _release_election_lock(lock_fd)
