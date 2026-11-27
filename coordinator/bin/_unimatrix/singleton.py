"""Per-box daemon singleton lock and owner-gone watcher for Unimatrix daemons.

Stdlib-only and inert on import. Neither the lock nor the watch spawns a process.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

from _unimatrix import proc as _proc
from _unimatrix import transport as _transport
from _unimatrix.paths import state_dir

HELD_RC = 75  # rc 3 is UNSUPPORTED_HOST_RC and 4 is MUX_UNSUPPORTED_RC in mirror-all
OWNER_POLL_S = 5.0

_held = []  # handles live for process life: the kernel lock dies with the open file
_last_role = None


class Handle:
    """One held singleton lock; keep it referenced, closing it releases the lock."""

    def __init__(self, role, identity, path, file) -> None:
        self.role = role
        self.identity = identity
        self.path = path
        self.file = file

    def close(self) -> None:
        try:
            _transport.unlock_file(self.file)
        except OSError:
            pass
        self.file.close()


def lock_path(role, identity) -> Path:
    """Lock file for one daemon identity under <settings home>/unimatrix/locks."""
    digest = hashlib.sha1(str(identity).encode("utf-8")).hexdigest()[:16]
    return state_dir() / "locks" / f"{role}-{digest}.lock"


def _holder_path(path) -> Path:
    return Path(path).with_suffix(".holder.json")


def holder_pid(path) -> int | None:
    """Pid recorded beside a lock file when it is a live process, else None (absent, torn or stale record)."""
    try:
        rec = json.loads(_holder_path(path).read_text(encoding="utf-8"))
        pid = rec["pid"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if isinstance(pid, int) and not isinstance(pid, bool) and _proc.pid_alive(pid):
        return pid
    return None


def hold(role, identity, *, path=None, windows=None) -> Handle:
    """Take the per-box lock for (role, identity) for process life.

    On contention prints `<role>: already running for <identity>, held by pid <N|unknown>` to stderr
    and exits HELD_RC. `path` overrides the lock file; `windows` forces a lock branch (test seam).
    """
    global _last_role
    lock = Path(path) if path is not None else lock_path(role, identity)
    lock.parent.mkdir(parents=True, exist_ok=True)
    file = open(lock, "a+b")
    try:
        got = _transport.lock_file(file, windows=windows)
    except BaseException:
        file.close()
        raise
    if not got:
        file.close()
        pid = holder_pid(lock)
        print(f"{role}: already running for {identity}, held by pid {pid if pid is not None else 'unknown'}",
              file=sys.stderr, flush=True)
        sys.exit(HELD_RC)
    rec = {"pid": os.getpid(), "procStart": owner_start(os.getpid()), "role": role, "identity": str(identity)}
    try:
        _holder_path(lock).write_text(json.dumps(rec), encoding="utf-8")
    except OSError:
        pass  # the lock, not the record, is the singleton; a loser then reports `unknown`
    handle = Handle(role, str(identity), lock, file)
    _held.append(handle)
    _last_role = role
    return handle


def owner_start(pid) -> str | None:
    """Start stamp of `pid`, or None when unreadable."""
    try:
        return _proc.proc_start(pid)
    except OSError:
        return None


def owner_gone(pid, start) -> bool:
    """True when `pid` is dead or its start stamp differs from `start`; an unreadable start is not gone."""
    if not _proc.pid_alive(pid):
        return True
    if start is None:
        return False
    current = owner_start(pid)
    return current is not None and current != start


def log_owner_gone(pid, role=None) -> None:
    """Write the `<role>: owner gone (pid N), exiting` line every owner-gone exit logs."""
    print(f"{role or _last_role or 'unimatrix'}: owner gone (pid {pid}), exiting", file=sys.stderr, flush=True)


def watch_owner(pid, start, on_gone, poll=OWNER_POLL_S, *, role=None) -> threading.Thread:
    """Poll `owner_gone` on a daemon thread; log and call `on_gone()` once, then stop."""
    role = role or _last_role

    def run() -> None:
        while True:
            time.sleep(poll)
            try:
                gone = owner_gone(pid, start)
            except OSError:
                continue
            if gone:
                log_owner_gone(pid, role)
                on_gone()
                return

    thread = threading.Thread(target=run, name="unimatrix-owner-watch", daemon=True)
    thread.start()
    return thread
