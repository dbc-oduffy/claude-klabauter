"""Box-wide TaskStop launch-record store and reaper log under settings_home().

One JSON file per background launch, keyed by task_id, plus an append-only reaper.jsonl.
Every function is fail-soft: a corrupt record is skipped, a refused task_id is a no-op/None.
"""

from __future__ import annotations

import json
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


def delete_record(task_id: str) -> None:
    if not valid_task_id(task_id):
        return
    try:
        _record_path(task_id).unlink()
    except OSError:
        pass


def _prune(now_s: float) -> None:
    # Trap: this runs on every launch hook. Opening each record to read its mark stalled the warm
    # engine past 10s at ~900 records (2026-11-03 stall dumps); scandir's stat carries the write
    # time with no open, and a record is written once, so mtime is its mark.
    import os

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
            delete_record(entry.name[: -len(".json")])


def write_record(rec: LaunchRecord, *, now_s: Optional[float] = None) -> bool:
    """Atomically persist `rec`, then prune records past RECORD_TTL_S. False when refused."""
    if not valid_task_id(rec.task_id):
        return False
    try:
        _store_dir().mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(_record_path(rec.task_id), json.dumps(asdict(rec)).encode("utf-8"))
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
