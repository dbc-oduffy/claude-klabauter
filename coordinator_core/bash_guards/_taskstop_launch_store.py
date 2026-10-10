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
from coordinator_core.bash_guards._taskstop_contract import (
    LOG_RELPATH,
    RECORD_TTL_S,
    STORE_RELPATH,
    LaunchRecord,
    ReapRow,
)

# task_id becomes a filename: no separator, dot or drive colon may pass.
_TASK_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# FILETIME ticks (100ns) between 1601-01-01 and the Unix epoch.
_FILETIME_UNIX_EPOCH = 116444736000000000


def _store_dir() -> Path:
    return settings_home() / STORE_RELPATH


def _log_path() -> Path:
    return settings_home() / LOG_RELPATH


def valid_task_id(task_id: object) -> bool:
    return isinstance(task_id, str) and _TASK_ID_RE.fullmatch(task_id) is not None


def _record_path(task_id: str) -> Path:
    return _store_dir() / f"{task_id}.json"


def _age_s(mark: int, now_s: float) -> float:
    return now_s - (mark - _FILETIME_UNIX_EPOCH) / 1e7


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
    for rec in all_records():
        if _age_s(rec.mark, now_s) > RECORD_TTL_S:
            delete_record(rec.task_id)


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
