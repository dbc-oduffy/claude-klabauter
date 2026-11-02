"""codex_runner.roster -- one JSON record per live codex worker, read by session.peer_roster.

Records live at `<settings_home>/worker-roster/<pid>.json`, keyed by the codex pid. A record
whose pid is dead is ignored by `live_records` (never deleted there: a reader owns no writes).
Trap: nothing sweeps records of a hard-killed worker, and a reused pid reads as live.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core._settings_home import settings_home

ROSTER_DIRNAME = "worker-roster"


def roster_dir() -> Path:
    return settings_home() / ROSTER_DIRNAME


def _record_path(pid: int) -> Path:
    return roster_dir() / f"{int(pid)}.json"


def _pid_alive(pid: int) -> bool:
    from coordinator_core.session import core

    try:
        return bool(core.pid_alive(pid))
    except Exception:
        return False


def write_record(
    pid: int,
    run_id: str,
    mode: str,
    cwd: str,
    parent_session_id: Optional[str] = None,
) -> Path:
    """Atomically write the roster record for codex process `pid`; returns its path."""
    path = _record_path(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    record: Dict[str, Any] = {
        "pid": int(pid),
        "run_id": run_id,
        "mode": mode,
        "cwd": cwd,
        "parent_session_id": parent_session_id,
        "start_epoch": time.time(),
    }
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    os.replace(tmp, path)
    return path


def remove_record(pid: int) -> None:
    """Delete the record for `pid`; absent is not an error."""
    try:
        _record_path(pid).unlink()
    except FileNotFoundError:
        pass


def live_records() -> List[Dict[str, Any]]:
    """Every readable record whose pid is alive; unreadable or dead-pid records are skipped."""
    out: List[Dict[str, Any]] = []
    try:
        entries = sorted(roster_dir().glob("*.json"))
    except OSError:
        return out
    for entry in entries:
        try:
            rec = json.loads(entry.read_text(encoding="utf-8"))
            pid = int(rec["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if isinstance(rec, dict) and _pid_alive(pid):
            out.append(rec)
    return out
