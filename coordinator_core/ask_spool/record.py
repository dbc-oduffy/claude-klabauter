"""Durable engine-ask submit spool: record path, id, exclusive write, claim.

The spool holds `hooks.submit_engine_ask` payloads the hook could not hand
to a healthy warm engine. Contract (shared with the C door writer, see
docs/reference/ask-spool.md):

- spool_id = sha1_hex16(session_id + tool_use_id), or, with no tool_use_id,
  sha1_hex16(session_id + raw payload bytes). Plain byte concatenation, no
  separator, matching door_core.c :: sha1_hex16.
- file = one header line (compact JSON) + "\\n" + the raw payload bytes.
- <root>/.coordinator-local/ask-spool/<id>.json, written as
  <id>.<pid>-<nonce>.tmp (O_CREAT|O_EXCL, mode 0600) then os.replace'd, so a
  reader of the Python writer's entries never sees a partial one (the C door
  writers still write <id>.json in place); an existing .json or a fresh .tmp
  of the same id means already spooled.
- states by rename: <id>.json -> <id>.claimed -> <id>.minted. A claimer also
  writes <id>.holder (its pid); a respool of an id with a .claimed or .minted
  is a no-op, and a .claimed is reclaimed only when older than CLAIM_STALE_S
  and its holder is provably dead (unreadable or ambiguous reads as alive).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import time
from typing import Optional

SPOOL_RELPATH = (".coordinator-local", "ask-spool")
PENDING_SUFFIX = ".json"
CLAIMED_SUFFIX = ".claimed"
MINTED_SUFFIX = ".minted"
TMP_SUFFIX = ".tmp"
HOLDER_SUFFIX = ".holder"
TMP_STALE_S = 60.0
CLAIM_STALE_S = 300.0
FILE_MODE = 0o600
DIR_MODE = 0o700
OP = "hooks.submit_engine_ask"


def spool_dir(repo_root: str) -> str:
    return os.path.join(repo_root, *SPOOL_RELPATH)


def entry_path(repo_root: str, spool_id: str, suffix: str = PENDING_SUFFIX) -> str:
    return os.path.join(spool_dir(repo_root), spool_id + suffix)


def spool_id_for(session_id: str, tool_use_id: Optional[str], payload: bytes) -> str:
    """sha1_hex16 of session_id + tool_use_id; payload bytes when no tool_use_id."""
    tail = tool_use_id.encode("utf-8") if tool_use_id else payload
    return hashlib.sha1(session_id.encode("utf-8") + tail).hexdigest()[:16]


def _stale(path: str, after_s: float = TMP_STALE_S) -> bool:
    """True when path's mtime is older than after_s; a vanished path is not stale."""
    try:
        return time.time() - os.stat(path).st_mtime > after_s
    except OSError:
        return False


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_INVALID_PARAMETER = 87
_FILETIME_UNIX_EPOCH = 116444736000000000


def win_pid_state(pid: int) -> Optional[bool]:
    """Windows proof of life via ctypes: True only when GetExitCodeProcess reads
    STILL_ACTIVE, False when OpenProcess says no such pid or the exit code is set,
    None when the kernel could not be asked (access denied, ctypes failure)."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False if ctypes.GetLastError() == _ERROR_INVALID_PARAMETER else None
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - the caller applies its own doubt policy
        return None


def posix_pid_state(pid: int) -> Optional[bool]:
    """os.kill(pid, 0): False on ESRCH, None on any other failure (EPERM
    included), True when the signal is deliverable."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return None
    return True


def pid_state(pid: int) -> Optional[bool]:
    """The one spawn-free liveness primitive: True alive, False provably gone,
    None doubt. Each caller maps None through its own policy."""
    return win_pid_state(pid) if sys.platform == "win32" else posix_pid_state(pid)


def win_pid_create_time(pid: int) -> Optional[float]:
    """Process creation instant (epoch seconds) from GetProcessTimes, or None."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            created, exited, kernel, user = (ctypes.c_ulonglong() for _ in range(4))
            if not kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel), ctypes.byref(user),
            ):
                return None
            return (created.value - _FILETIME_UNIX_EPOCH) / 1e7
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return None


def posix_pid_create_time(pid: int) -> Optional[float]:
    """Process creation instant (epoch seconds) via psutil (procfs/sysctl, no spawn), or None."""
    try:
        import psutil

        return float(psutil.Process(pid).create_time())
    except Exception:  # noqa: BLE001
        return None


def pid_create_time(pid: int) -> Optional[float]:
    """Creation instant of pid, for a pid-reuse guard; None when unreadable."""
    return win_pid_create_time(pid) if sys.platform == "win32" else posix_pid_create_time(pid)


def _pid_alive(pid: int) -> bool:
    """Any doubt reads as alive."""
    if pid <= 0 or pid == os.getpid():
        return True
    return pid_state(pid) is not False


def _holder_dead(repo_root: str, spool_id: str) -> bool:
    """True only when <id>.holder names a pid that is provably gone."""
    try:
        with open(entry_path(repo_root, spool_id, HOLDER_SUFFIX), "rb") as fh:
            pid = int(fh.read(32).strip())
    except (OSError, ValueError):
        return False
    return not _pid_alive(pid)


def _drop_holder(repo_root: str, spool_id: str) -> None:
    try:
        os.unlink(entry_path(repo_root, spool_id, HOLDER_SUFFIX))
    except OSError:
        pass


def _live_claim(repo_root: str, spool_id: str) -> bool:
    """True when <id>.claimed exists and is not a provably dead claimer's leftover;
    a dead one lets a respool recreate <id>.json so claim() can reclaim it."""
    claimed = entry_path(repo_root, spool_id, CLAIMED_SUFFIX)
    if not os.path.exists(claimed):
        return False
    return not (_stale(claimed, CLAIM_STALE_S) and _holder_dead(repo_root, spool_id))


def _tmp_fresh_peer(directory: str, spool_id: str) -> bool:
    """Reap orphaned tmps of this id (each writer's tmp name is unique, so only
    age proves orphanhood); True when a fresh tmp, i.e. a live writer, remains."""
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return False
    fresh = False
    for name in names:
        if not name.endswith(TMP_SUFFIX):
            continue
        stem = name[: -len(TMP_SUFFIX)]
        if stem != spool_id and not stem.startswith(spool_id + "."):
            continue
        path = os.path.join(directory, name)
        if _stale(path):
            try:
                os.unlink(path)
            except OSError:
                pass
        elif os.path.exists(path):
            fresh = True
    return fresh


def spool_ask(
    repo_root: str,
    payload: bytes,
    *,
    session_id: str,
    tool_use_id: Optional[str] = None,
    reason: str,
    op: str = OP,
    spooled_at: Optional[float] = None,
) -> tuple[str, bool]:
    """Write one entry; return (spool_id, created). created False = already spooled."""
    spool_id = spool_id_for(session_id, tool_use_id, payload)
    header = {
        "spool_id": spool_id,
        "op": op,
        "reason": reason,
        "spooled_at": time.time() if spooled_at is None else spooled_at,
        "session_id": session_id,
        "repo_root": repo_root,
    }
    body = json.dumps(header, separators=(",", ":")).encode("utf-8") + b"\n" + payload
    os.makedirs(spool_dir(repo_root), mode=DIR_MODE, exist_ok=True)
    final = entry_path(repo_root, spool_id)
    if (
        os.path.exists(final)
        or _live_claim(repo_root, spool_id)
        or os.path.exists(entry_path(repo_root, spool_id, MINTED_SUFFIX))
        or _tmp_fresh_peer(spool_dir(repo_root), spool_id)
    ):
        return spool_id, False
    tmp = entry_path(
        repo_root, spool_id, f".{os.getpid()}-{secrets.token_hex(4)}{TMP_SUFFIX}"
    )
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    except FileExistsError:
        return spool_id, False
    try:
        try:
            view = memoryview(body)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
        if os.path.exists(final):
            os.unlink(tmp)
            return spool_id, False
        try:
            os.replace(tmp, final)
        except FileNotFoundError:
            return spool_id, False
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return spool_id, True


def read_entry(path: str) -> tuple[dict, bytes]:
    """Return (header, raw payload bytes); ValueError on a malformed entry."""
    with open(path, "rb") as fh:
        data = fh.read()
    head, sep, payload = data.partition(b"\n")
    if not sep:
        raise ValueError(f"ask-spool entry has no header line: {path}")
    try:
        header = json.loads(head)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ask-spool entry header is not JSON: {path}") from exc
    if not isinstance(header, dict) or "spool_id" not in header:
        raise ValueError(f"ask-spool entry header lacks spool_id: {path}")
    return header, payload


def list_pending(repo_root: str) -> list[str]:
    """Spool ids with a pending <id>.json, oldest name-order first."""
    try:
        names = os.listdir(spool_dir(repo_root))
    except FileNotFoundError:
        return []
    return sorted(n[: -len(PENDING_SUFFIX)] for n in names if n.endswith(PENDING_SUFFIX))


def claim(repo_root: str, spool_id: str) -> Optional[str]:
    """Rename <id>.json to <id>.claimed; the claimed path, None if another drainer won."""
    src = entry_path(repo_root, spool_id)
    dst = entry_path(repo_root, spool_id, CLAIMED_SUFFIX)
    for _ in range(2):
        try:
            os.rename(src, dst)
        except FileNotFoundError:
            return None
        except PermissionError as exc:
            if os.path.exists(src):
                print(f"ask_spool.claim: permission denied renaming {src}: {exc}", file=sys.stderr)
            return None
        except FileExistsError:
            if not _reclaim_stale_claimed(repo_root, spool_id, dst):
                return None
        else:
            try:
                os.utime(dst)
                fd = os.open(
                    entry_path(repo_root, spool_id, HOLDER_SUFFIX),
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    FILE_MODE,
                )
                with os.fdopen(fd, "w") as fh:
                    fh.write(str(os.getpid()))
            except OSError:
                pass
            return dst
    return None


def _reclaim_stale_claimed(repo_root: str, spool_id: str, claimed: str) -> bool:
    """Drop a dead claimer's leftover <id>.claimed (Windows rename refuses to
    overwrite it); rename-aside first so one reclaimer wins. False unless the
    claim is older than CLAIM_STALE_S and its holder is provably dead."""
    if not _stale(claimed, CLAIM_STALE_S) or not _holder_dead(repo_root, spool_id):
        return False
    aside = f"{claimed}.reclaim-{os.getpid()}-{secrets.token_hex(4)}"
    try:
        os.rename(claimed, aside)
    except OSError:
        return True
    try:
        os.unlink(aside)
    except OSError:
        pass
    _drop_holder(repo_root, spool_id)
    return True


def release(repo_root: str, spool_id: str) -> None:
    """Return a claimed entry to pending after a failed mint, so it is retried.

    A pending entry of the same id already present means the retry is queued:
    the claimed copy is dropped instead of raising.
    """
    claimed = entry_path(repo_root, spool_id, CLAIMED_SUFFIX)
    _drop_holder(repo_root, spool_id)
    try:
        os.rename(claimed, entry_path(repo_root, spool_id))
    except FileExistsError:
        try:
            os.unlink(claimed)
        except FileNotFoundError:
            pass


def mark_minted(repo_root: str, spool_id: str) -> str:
    """Rename <id>.claimed to <id>.minted after the run is minted."""
    dst = entry_path(repo_root, spool_id, MINTED_SUFFIX)
    os.rename(entry_path(repo_root, spool_id, CLAIMED_SUFFIX), dst)
    _drop_holder(repo_root, spool_id)
    return dst
