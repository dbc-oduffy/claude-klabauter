"""Box-wide TaskStop launch-record store and reaper log under settings_home().

One JSON file per background launch, keyed by task_id, plus an append-only reaper.jsonl.
Every function is fail-soft: a corrupt record is skipped, a refused task_id is a no-op/None.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.atomic_append import append_line
from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.bash_guards._taskstop_attribution import POST_WINDOW_S, PRE_SLACK_S
from coordinator_core.bash_guards._taskstop_contract import (
    FILETIME_UNIX_EPOCH,
    LOG_RELPATH,
    RECORD_TTL_S,
    STORE_RELPATH,
    LaunchRecord,
    ReapRow,
)

# FAT mtime granularity.
_MTIME_SLACK_S = 2.0

# task_id becomes a filename: no separator, dot or drive colon may pass.
_TASK_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# Per-agent index of timed-out records: by-agent/<hash>/<task_id>, empty marker files.
_MARKER_DIRNAME = "by-agent"


def _store_dir() -> Path:
    return settings_home() / STORE_RELPATH


def _log_path() -> Path:
    return settings_home() / LOG_RELPATH


def valid_task_id(task_id: object) -> bool:
    return isinstance(task_id, str) and _TASK_ID_RE.fullmatch(task_id) is not None


def _record_path(task_id: str) -> Path:
    return _store_dir() / f"{task_id}.json"


def _age_s(mark: int, now_s: float) -> float:
    return now_s - (mark - FILETIME_UNIX_EPOCH) / 1e7


def _load(path: Path) -> Optional[LaunchRecord]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return LaunchRecord(
            task_id=str(d["task_id"]),
            session_id=str(d["session_id"]),
            tool_use_id=str(d["tool_use_id"]),
            command=str(d["command"]),
            mark=int(d["mark"]),
            agent_id=str(d.get("agent_id", "")),
            timed_out=bool(d.get("timed_out", False)),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def all_records() -> List[LaunchRecord]:
    try:
        paths = sorted(_store_dir().glob("*.json"))
    except OSError:
        return []
    out = []
    for p in paths:
        rec = _load(p)
        if rec is not None:
            out.append(rec)
    return out


def records_near(mark: int) -> List[LaunchRecord]:
    """Records that can compete with a launch at `mark`: opens only files not older than the window."""
    import os

    # Invariant: a record is written at or after its mark, so mtime >= mark. Never add an upper
    # bound: a timed-out record is written timedOutAfterMs after its mark.
    floor = (
        (mark - FILETIME_UNIX_EPOCH) / 1e7 - (PRE_SLACK_S + POST_WINDOW_S) - _MTIME_SLACK_S
    )
    try:
        entries = sorted(os.scandir(_store_dir()), key=lambda e: e.name)
    except OSError:
        return []
    out = []
    for entry in entries:
        if not entry.name.endswith(".json"):
            continue
        try:
            if entry.stat().st_mtime < floor:
                continue
        except OSError:
            pass  # unknown age: keep it, a dropped sibling would turn ambiguity into a kill
        rec = _load(Path(entry.path))
        if rec is not None:
            out.append(rec)
    return out


def read_record(task_id: str) -> Optional[LaunchRecord]:
    if not valid_task_id(task_id):
        return None
    return _load(_record_path(task_id))


def _marker_dir(session_id: str, agent_id: str) -> Path:
    key = hashlib.sha256(f"{session_id}\0{agent_id}".encode("utf-8")).hexdigest()[:16]
    return _store_dir() / _MARKER_DIRNAME / key


def records_for_agent(session_id: str, agent_id: str) -> List[LaunchRecord]:
    """Timed-out records launched by (session_id, agent_id), sorted by task_id.

    Opens only the records named in that agent's marker dir, never the whole store."""
    if not session_id or not agent_id:
        return []
    mdir = _marker_dir(session_id, agent_id)
    try:
        names = sorted(e.name for e in os.scandir(mdir))
    except OSError:
        return []
    out = []
    for name in names:
        if not valid_task_id(name):
            continue
        rec = _load(_record_path(name))
        if rec is None:
            try:
                (mdir / name).unlink()
            except OSError:
                pass
            continue
        if rec.session_id == session_id and rec.agent_id == agent_id and rec.timed_out:
            out.append(rec)
    return out


def delete_record(task_id: str) -> None:
    if not valid_task_id(task_id):
        return
    rec = _load(_record_path(task_id))
    try:
        _record_path(task_id).unlink()
    except OSError:
        pass
    if rec is not None and rec.timed_out and rec.agent_id:
        try:
            (_marker_dir(rec.session_id, rec.agent_id) / task_id).unlink()
        except OSError:
            pass


def _prune(now_s: float) -> None:
    # Trap: this runs on every launch hook. Opening each record to read its mark stalled the warm
    # engine past 10s at ~900 records (2026-11-03 stall dumps); scandir's stat carries the write
    # time with no open, and a record is written once, so mtime is its mark.
    try:
        entries = list(os.scandir(_store_dir()))
    except OSError:
        return
    for entry in entries:
        if not entry.name.endswith(".json"):
            continue
        try:
            stale = now_s - entry.stat().st_mtime > RECORD_TTL_S
        except OSError:
            continue
        if stale:
            try:
                (_store_dir() / entry.name).unlink()
            except OSError:
                pass
    _prune_markers(now_s)


def _prune_markers(now_s: float) -> None:
    try:
        agent_dirs = [e for e in os.scandir(_store_dir() / _MARKER_DIRNAME) if e.is_dir()]
    except OSError:
        return
    for adir in agent_dirs:
        try:
            markers = list(os.scandir(adir.path))
        except OSError:
            continue
        live = 0
        for m in markers:
            try:
                if now_s - m.stat().st_mtime > RECORD_TTL_S:
                    os.unlink(m.path)
                else:
                    live += 1
            except OSError:
                live += 1
        if not live:
            try:
                os.rmdir(adir.path)
            except OSError:
                pass


def write_record(rec: LaunchRecord, *, now_s: Optional[float] = None) -> bool:
    """Atomically persist `rec`, then prune records past RECORD_TTL_S. False when refused."""
    if not valid_task_id(rec.task_id):
        return False
    try:
        _store_dir().mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(_record_path(rec.task_id), json.dumps(asdict(rec)).encode("utf-8"))
        if rec.timed_out and rec.agent_id:
            mdir = _marker_dir(rec.session_id, rec.agent_id)
            mdir.mkdir(parents=True, exist_ok=True)
            (mdir / rec.task_id).touch()
        _prune(time.time() if now_s is None else now_s)
    except OSError:
        return False
    return True


def append_row(row: ReapRow) -> bool:
    """Append one jsonl line to the reaper log."""
    d = {
        "ts": time.time(),
        "task_id": row.task_id,
        "outcome": row.outcome.value,
        "killed": [list(k) for k in row.killed],
        "competing": list(row.competing),
        "candidates": row.candidates,
    }
    try:
        _log_path().parent.mkdir(parents=True, exist_ok=True)
        append_line(_log_path(), (json.dumps(d) + "\n").encode("utf-8"))
    except OSError:
        return False
    return True
