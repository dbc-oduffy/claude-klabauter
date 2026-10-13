"""coordinator_core.hooks.dispatch_escape -- session-id, sentinel-reason and escape-log helpers
shared by the EM dispatch guards. Imports no mode resolution."""

from __future__ import annotations

import json
import os
import re
import socket
from datetime import datetime, timezone


def _sanitize_for_hostname(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "-", s)


def _resolve_session_id_op(payload: dict) -> str:
    sid = payload.get("session_id")
    if isinstance(sid, str) and sid.strip():
        safe = re.sub(r"[^A-Za-z0-9_-]", "", sid.strip())
        if safe:
            return safe
    hostname = _sanitize_for_hostname(socket.gethostname())
    return f"{hostname}-{os.getpid()}"


ESCAPE_LOG_NAME = "dispatch-escapes.jsonl"


def _sentinel_reason(path: str) -> str:
    """The sentinel's reason line; empty when absent, empty, or unreadable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.strip():
                    return line.strip()
    except OSError:
        pass
    return ""


def _log_escape(payload: dict, session_id: str, file_path: str, reason: str) -> None:
    """Append {ts, session_id, path, reason} to the per-session escape log."""
    try:
        from coordinator_core.hooks import nudge_unrouted_sizing as sizing
        from coordinator_core.session.machinery_paths import share_dir

        repo_root = sizing._repo_root({"cwd": payload.get("cwd") or os.getcwd()})
        if not repo_root:
            return
        d = share_dir(repo_root, session_id)
        os.makedirs(d, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session_id": session_id,
            "path": file_path,
            "reason": reason,
        }
        with open(os.path.join(d, ESCAPE_LOG_NAME), "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


_FAILED_STATUSES = frozenset({"blocked", "thrashing", "aborted", "failed"})


def _executor_failed(payload: dict, session_id: str) -> bool:
    """True iff a run-report sidecar of this session names a failed executor."""
    try:
        from coordinator_core.hooks import nudge_unrouted_sizing as sizing
        from coordinator_core.session.machinery_paths import share_dirs

        repo_root = sizing._repo_root({"cwd": payload.get("cwd") or os.getcwd()})
        if not repo_root:
            return False
        for d in share_dirs(repo_root, session_id):
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for name in names:
                if not name.endswith(".md"):
                    continue
                with open(os.path.join(d, name), "r", encoding="utf-8", errors="replace") as fh:
                    head = fh.read(1024)
                status = agent = ""
                for line in head.splitlines()[1:]:
                    if line.startswith("---"):
                        break
                    key, _, val = line.partition(":")
                    if key == "status":
                        status = val.strip().strip("'\"").casefold()
                    elif key == "agent_type":
                        agent = val.strip().strip("'\"").casefold()
                if status in _FAILED_STATUSES and agent.endswith("executor"):
                    return True
    except Exception:
        return False
    return False


def _take_escape(payload: dict, session_id: str, subject: str, guard_name: str = "") -> bool:
    """Honour an escape once: a live PM unlock for ``guard_name``, or a sentinel
    reason line while a dispatched executor has ended BLOCKED/failed.

    A bare reason is not honoured and the sentinel is left in place.
    """
    from coordinator_core.session import guard_unlock_sentinel
    from coordinator_core.session.dispatch_nudge_sentinel import sentinel_path

    if guard_name and guard_unlock_sentinel.consume(session_id, guard_name):
        _log_escape(payload, session_id, subject, f"pm-unlock:{guard_name}")
        return True
    path = str(sentinel_path(session_id))
    reason = _sentinel_reason(path)
    if not reason or not _executor_failed(payload, session_id):
        return False
    _log_escape(payload, session_id, subject, reason)
    try:
        os.remove(path)
    except OSError:
        pass
    return True
