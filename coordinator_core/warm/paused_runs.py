"""Paused-run store and pause/handover markers for a publish that pauses live workflow runs.

Records live at <repo>/.coordinator-local/paused-runs/<run_id>.json; the markers pause-request.json and
handover-complete.json sit beside them. Pure file IO: no spawn, no engine call (the engine is down
mid-swap). The resume handle (script_path, args) comes from the run record postuse_advisory_dispatch
captures; render_resume_call (workflow_watch.stall) renders the resume call from a paused record.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

PAUSED_DIR_REL = Path(".coordinator-local") / "paused-runs"
PAUSE_REQUEST_NAME = "pause-request.json"
HANDOVER_COMPLETE_NAME = "handover-complete.json"

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MARKER_NAMES = frozenset({PAUSE_REQUEST_NAME, HANDOVER_COMPLETE_NAME})

RepoLike = Union[str, "os.PathLike[str]"]


def paused_dir(repo: RepoLike) -> Path:
    return Path(repo) / PAUSED_DIR_REL


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _record_path(repo: RepoLike, run_id: str) -> Path:
    # run_id becomes a filename: reject anything that could escape the directory or collide with a marker.
    if (not isinstance(run_id, str) or not _SAFE_ID_RE.match(run_id)
            or f"{run_id}.json" in _MARKER_NAMES):
        raise ValueError(f"unsafe run_id: {run_id!r}")
    return paused_dir(repo) / f"{run_id}.json"


def write_paused(
    repo: RepoLike,
    *,
    run_id: str,
    script_path: Optional[str],
    args: Any,
    task_id: Optional[str] = None,
    session_id: Optional[str] = None,
    paused_at: Optional[str] = None,
) -> Path:
    """Record a paused run (pending resume); rewriting the same run_id replaces the record."""
    path = _record_path(repo, run_id)
    _atomic_write(path, {
        "run_id": run_id,
        "script_path": script_path,
        "args": args,
        "task_id": task_id,
        "session_id": session_id,
        "repo": str(repo),
        "paused_at": paused_at or _now_iso(),
        "resumed_at": None,
    })
    return path


def read_paused(repo: RepoLike, run_id: str) -> Optional[dict]:
    return _read(_record_path(repo, run_id))


def list_pending(repo: RepoLike) -> list[dict]:
    """Paused records not yet resumed, oldest first; markers, tmp files and unreadable records are skipped."""
    d = paused_dir(repo)
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = []
    for name in names:
        if not name.endswith(".json") or name in _MARKER_NAMES:
            continue
        rec = _read(d / name)
        if rec is None or not rec.get("run_id") or rec.get("resumed_at"):
            continue
        out.append(rec)
    out.sort(key=lambda r: (str(r.get("paused_at") or ""), str(r["run_id"])))
    return out


def mark_resumed(repo: RepoLike, run_id: str, resumed_at: Optional[str] = None) -> bool:
    """Stamp resumed_at on the record; False when no readable record exists."""
    path = _record_path(repo, run_id)
    rec = _read(path)
    if rec is None:
        return False
    rec["resumed_at"] = resumed_at or _now_iso()
    _atomic_write(path, rec)
    return True


def _write_marker(repo: RepoLike, name: str, payload: dict) -> Path:
    path = paused_dir(repo) / name
    _atomic_write(path, {**payload, "at": _now_iso()})
    return path


def request_pause(repo: RepoLike, *, requested_by: Optional[str] = None) -> Path:
    """Ask live runs in this repo to pause for an engine swap."""
    return _write_marker(repo, PAUSE_REQUEST_NAME, {"requested_by": requested_by})


def pause_requested(repo: RepoLike) -> Optional[dict]:
    return _read(paused_dir(repo) / PAUSE_REQUEST_NAME)


def clear_pause_request(repo: RepoLike) -> bool:
    try:
        (paused_dir(repo) / PAUSE_REQUEST_NAME).unlink()
    except FileNotFoundError:
        return False
    return True


def mark_handover_complete(repo: RepoLike, *, engine: Optional[str] = None) -> Path:
    """Signal the new engine is serving, so paused runs may resume."""
    return _write_marker(repo, HANDOVER_COMPLETE_NAME, {"engine": engine})


def handover_complete(repo: RepoLike) -> Optional[dict]:
    return _read(paused_dir(repo) / HANDOVER_COMPLETE_NAME)


def clear_handover_complete(repo: RepoLike) -> bool:
    try:
        (paused_dir(repo) / HANDOVER_COMPLETE_NAME).unlink()
    except FileNotFoundError:
        return False
    return True
