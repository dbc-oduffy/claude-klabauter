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


_RUNS_DIR = ("scratch", "warp")
_DISPATCH_REPORTS_DIR = (".coordinator-local", "subagent-share", "dispatch-reports")


def _first_line_failed(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return False
    key, _, val = first.partition(":")
    return key.strip().casefold() == "status" and val.strip().strip("'\"").casefold() in _FAILED_STATUSES


def _in_run_report_failed(repo_root: str, session_id: str) -> bool:
    """True iff a run this session submitted left a failed dispatch report.

    A run belongs to the session through its ``submission.json`` session_id; its report
    rows come from ``manifest.json`` and only reports written after submission count.
    """
    reports = os.path.join(repo_root, *_DISPATCH_REPORTS_DIR)
    try:
        plan_dirs = [e.path for e in os.scandir(reports) if e.is_dir()]
        runs = [e.path for e in os.scandir(os.path.join(repo_root, *_RUNS_DIR)) if e.is_dir()]
    except OSError:
        return False
    for run in runs:
        try:
            with open(os.path.join(run, "submission.json"), "r", encoding="utf-8") as fh:
                sub = json.load(fh)
            if sub.get("session_id") != session_id:
                continue
            submitted = float(sub.get("submitted_at") or 0)
            with open(os.path.join(run, "manifest.json"), "r", encoding="utf-8") as fh:
                rows = json.load(fh).get("rows") or []
        except (OSError, ValueError, AttributeError, TypeError):
            continue
        for row in rows:
            row_id = row.get("id") if isinstance(row, dict) else None
            if not isinstance(row_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", row_id):
                continue
            for plan_dir in plan_dirs:
                report = os.path.join(plan_dir, f"{row_id}.md")
                try:
                    fresh = os.path.getmtime(report) >= submitted
                except OSError:
                    continue
                if fresh and _first_line_failed(report):
                    return True
    return False


def _executor_failed(payload: dict, session_id: str) -> bool:
    """True iff a run-report sidecar of this session, or an in-run dispatch report, names a failed executor."""
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
        return _in_run_report_failed(repo_root, session_id)
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
