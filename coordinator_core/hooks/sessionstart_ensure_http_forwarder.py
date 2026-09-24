"""coordinator_core.hooks.sessionstart_ensure_http_forwarder — SessionStart(*)
op: ensures the resident http hook forwarder is up, without waiting on it.

Arrival note (W4-C10, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude
`coordinator/hooks/scripts/sessionstart-ensure-http-forwarder.py`. The source
script is documented as deliberately NOT importing `coordinator_core` and NOT
resolving an engine root — its whole job is arbitrating over the forwarder's
own exclusive-bind primitive and, on a fingerprint mismatch, retiring a stale
process and spawning a successor. That contract is unchanged here; this is a
near-verbatim port.

ADAPTATION (class 1): the ONE thing the source script resolved DoE-plane-
locally was `_FORWARDER_MODULE_PATH`
(`Path(__file__).resolve().parents[1] / "http_hook_forwarder.py"` — a sibling
in DoE's own `coordinator/hooks/` tree). This op's own `__file__` has no such
sibling (the forwarder body is doctrine-plane content, out of this chunk's
footprint) — resolved instead via `CLAUDE_PLUGIN_ROOT` (`<plugin_root>/hooks/
http_hook_forwarder.py`), the same doctrine-asset resolution convention every
other arrival in this row uses for displaced doctrine-plane content. Returns
None (never spawns) when `CLAUDE_PLUGIN_ROOT` is absent or the forwarder file
does not resolve under it.

Liveness/identity confirmation for an already-bound forwarder is spawn-free
(`_pid_is_a_forwarder`, POSIX `os.kill(pid, 0)` + `/proc` cmdline, Windows
`ctypes` `OpenProcess`/`QueryFullProcessImageNameW`) — no `ps`/`powershell.exe`
child process, no console flash, no `no_console_creationflags()` need on this
leg. Spawning the forwarder ITSELF (`_spawn_forwarder_detached`) still uses
`subprocess.Popen` with its own inline no-console/detached flags, since
starting the forwarder is the op's actual job.

Op contract: `params` is unused. Fails open on every path: probe-bind
success, probe-bind loss (already running), spawn failure, or an unexpected
exception all return `no_advisory()`; a genuine failure that must be
disclosed returns `context_only("SessionStart", ...)` carrying the same
stderr-mirrored diagnostic text the source script wrote to both channels
(mirrors that script's "DISCLOSE LOUDLY, NEVER SILENTLY, ON FAILURE"
contract — this op has no separate stderr channel of its own, so the
diagnostic goes out via `additionalContext` only).

Negative-spec:
    Does NOT import `coordinator_core` beyond this package's own envelope/
    register_op seams, and does NOT resolve an engine root — matches the
    source script's own negative-spec verbatim; starting the forwarder needs
    only the doctrine-plane sibling file, never the engine.
    Does NOT health-check, poll, or retry the forwarder once spawned or
    found already bound beyond the one code-identity comparison this module
    already makes.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C10
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from coordinator_core.hooks._envelope import context_only, no_advisory
from coordinator_core.ipc import register_op

#: Mirrors `http_hook_forwarder.FIXED_PORT` by value, not by import -- this
#: module must not import the forwarder module itself (it only launches it as
#: a detached child process).
_FIXED_PORT = 47623

_ADDR_IN_USE_ERRNOS = frozenset(
    e
    for e in (
        getattr(__import__("errno"), "EADDRINUSE", None),
        10048,  # WSAEADDRINUSE, Windows
    )
    if e is not None
)


def _forwarder_module_path() -> "Optional[Path]":
    """`<plugin_root>/hooks/http_hook_forwarder.py`, or None -- see module
    docstring ADAPTATION."""
    raw = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not raw:
        return None
    try:
        candidate = Path(raw) / "hooks" / "http_hook_forwarder.py"
    except Exception:
        return None
    return candidate


def _probe_bind_wins(port: int = _FIXED_PORT) -> "Optional[bool]":
    """Attempt an exclusive probe bind on `port`, immediately releasing it on
    success. Returns True (this call won -- caller should spawn), False
    (lost to an existing listener -- caller should do nothing), or None (an
    unexpected error -- caller should disclose a failure). Never raises."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        exclusive_flag = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive_flag is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, exclusive_flag, 1)
            except OSError:
                pass  # Windows-only exclusive-bind flag; absence just loses the stronger race guarantee
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            if getattr(exc, "errno", None) in _ADDR_IN_USE_ERRNOS:
                return False
            return None
        return True
    except Exception:
        return None
    finally:
        try:
            sock.close()
        except Exception:
            pass  # teardown of a probe socket that is already going out of scope


def _dial_count_path() -> Path:
    override = os.environ.get("COORDINATOR_FORWARDER_DIAL_COUNT_PATH")
    if override and override.strip():
        return Path(override.strip())
    base = os.environ.get("CLAUDE_HOME") or str(Path.home())
    return Path(base) / ".claude" / "http-hook-forwarder-dial-count.json"


def _module_fingerprint_on_disk(forwarder_path: Path) -> "Optional[str]":
    try:
        return hashlib.sha256(forwarder_path.read_bytes()).hexdigest()[:16]
    except Exception:
        return None


def _running_forwarder_record() -> "Optional[dict]":
    try:
        with _dial_count_path().open("r", encoding="utf-8") as handle:
            record = json.load(handle)
    except Exception:
        return None
    return record if isinstance(record, dict) else None


#: A command line has to carry the module's own filename as a whole path
#: component to count as a forwarder worth SIGTERMing -- a bare substring
#: match would also hit a test file, an editor, or a grep.
_FORWARDER_ARGV_RE = re.compile(r"""(?:^|[\s"'/\\])http_hook_forwarder\.py(?:["'\s]|$)""")


def _pid_is_a_forwarder(pid: int) -> "Optional[bool]":
    """Confirm `pid` against the OS process table before it is ever killed --
    spawn-free (overengineering-reviewer, 2026-09-18: the prior `ps`/
    `powershell.exe Get-CimInstance` spawn ran on every SessionStart where the
    resident forwarder's fingerprint had gone stale; a PowerShell spawn alone
    costs more than this engine's 200ms single-process bar). POSIX confirms
    liveness via `os.kill(pid, 0)` (signal 0: existence-only, never delivered)
    and cross-checks the command line via `/proc/<pid>/cmdline` where present
    (Linux; a no-op elsewhere). Windows confirms via `ctypes`
    `OpenProcess`/`QueryFullProcessImageNameW` against `kernel32`, no shell.

    Returns True/False when identity could be confirmed, None when it could
    not be (caller treats None as do-not-kill)."""
    if os.name == "nt":
        return _pid_is_a_forwarder_windows(pid)
    return _pid_is_a_forwarder_posix(pid)


def _pid_is_a_forwarder_posix(pid: int) -> "Optional[bool]":
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive, owned by another user -- exists, but we cannot read its
        # cmdline to confirm identity.
        return None
    except Exception:
        return None

    cmdline_path = Path("/proc") / str(pid) / "cmdline"
    try:
        raw = cmdline_path.read_bytes()
    except Exception:
        # No /proc (e.g. macOS) -- liveness confirmed, identity not; treat as
        # do-not-kill rather than assume.
        return None
    argv_text = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
    return bool(_FORWARDER_ARGV_RE.search(argv_text))


def _pid_is_a_forwarder_windows(pid: int) -> "Optional[bool]":
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not handle:
            return False
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            ok = kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)
            )
            if not ok:
                return None
            return bool(_FORWARDER_ARGV_RE.search(buf.value))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


_BIND_CONFIRM_ATTEMPTS = 10
_BIND_CONFIRM_INTERVAL_SECS = 0.2


def _forwarder_is_listening(port: int = _FIXED_PORT) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def _await_successor_bind() -> bool:
    for _ in range(_BIND_CONFIRM_ATTEMPTS):
        if _forwarder_is_listening():
            return True
        time.sleep(_BIND_CONFIRM_INTERVAL_SECS)
    return _forwarder_is_listening()


def _retire_stale_forwarder(pid: int) -> bool:
    try:
        os.kill(int(pid), signal.SIGTERM)
        return True
    except Exception:
        return False


def _spawn_forwarder_detached(forwarder_path: Path) -> bool:
    if not forwarder_path.is_file():
        return False

    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    detached = getattr(subprocess, "DETACHED_PROCESS", 0)
    creationflags = no_window | detached

    kwargs: dict = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    if os.name == "nt":
        kwargs["creationflags"] = creationflags
    else:
        kwargs["start_new_session"] = True

    try:
        # popup-safe-env-suppressed -- CREATE_NO_WINDOW is already ORed into
        # creationflags above (Windows leg); DETACHED_PROCESS additionally
        # detaches from this session's own console entirely.
        subprocess.Popen([sys.executable, str(forwarder_path)], **kwargs)
        return True
    except Exception:
        return False


def _ensure_current_forwarder(forwarder_path: Path) -> "Optional[str]":
    """Handle the already-bound case: retire and replace the winner IFF it
    runs superseded code. Returns a disclosure reason string, or None when
    nothing needed disclosing."""
    on_disk = _module_fingerprint_on_disk(forwarder_path)
    if on_disk is None:
        return None
    record = _running_forwarder_record()
    if record is None:
        return None
    running = record.get("module_fingerprint")
    if not isinstance(running, str) or not running:
        return None
    if running == on_disk:
        return None
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if _pid_is_a_forwarder(pid) is not True:
        return (
            "the resident forwarder runs superseded code (running {0}, on disk {1}) but pid {2} "
            "could not be confirmed to be a forwarder -- left running, restart it by hand".format(
                running, on_disk, pid
            )
        )
    if not _retire_stale_forwarder(pid):
        return (
            "the resident forwarder runs superseded code (running {0}, on disk {1}) and pid {2} "
            "could not be terminated -- left running, restart it by hand".format(
                running, on_disk, pid
            )
        )
    if not _spawn_forwarder_detached(forwarder_path):
        return (
            "retired the superseded forwarder at pid {0} but failed to spawn its successor -- THE "
            "BOX HAS NO FORWARDER AND BASH GUARDS ARE FAILING OPEN until one binds".format(pid)
        )
    if _await_successor_bind():
        return None
    if _spawn_forwarder_detached(forwarder_path) and _await_successor_bind():
        return None
    return (
        "retired the superseded forwarder at pid {0} and its successor did not take port {1} "
        "(launched, then lost the bind or exited) -- THE BOX HAS NO FORWARDER AND BASH GUARDS ARE "
        "FAILING OPEN until one binds".format(pid, _FIXED_PORT)
    )


def _disclose(reason: str) -> dict:
    return context_only(
        "SessionStart",
        f"HTTP hook forwarder not ensured: {reason}. Bash-guard http calls may "
        "find no backend this session.",
    )


@register_op("hooks.sessionstart_ensure_http_forwarder")
def _handler(params: dict, repo_root=None) -> dict:
    try:
        forwarder_path = _forwarder_module_path()
        if forwarder_path is None:
            return no_advisory()

        result = _probe_bind_wins()
        if result is False:
            reason = _ensure_current_forwarder(forwarder_path)
            return _disclose(reason) if reason else no_advisory()
        if result is True:
            if not _spawn_forwarder_detached(forwarder_path):
                return _disclose("won the probe bind but failed to spawn the forwarder process")
            return no_advisory()
        return _disclose("probe bind failed for a reason other than address-in-use")
    except Exception as exc:
        return _disclose(f"unexpected error ensuring the http hook forwarder: {exc!r}")
