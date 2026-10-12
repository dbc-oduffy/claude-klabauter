"""Single-flight admission for a ceremony run: the once-per-day marker check plus a run-scoped lock.

The lock lives under `<git-common-dir>/coordinator-sessions/<ceremony>-runs/`. No timeout takeover and
no force flag exist: a lock is taken over only when its holder is provably dead (`launching`) or
dated before today (`awaiting-finish`). An unreadable or undeterminable holder counts as alive.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from coordinator_core.daily_day import local_day
from coordinator_core.git.git_dir import resolve_git_common_dir

ADMITTED = "admitted"
IN_FLIGHT = "in-flight"
ALREADY_RAN = "already-ran"

PHASE_LAUNCHING = "launching"
PHASE_AWAITING_FINISH = "awaiting-finish"

_LOCK_NAME = "lock.json"


@dataclass(frozen=True)
class Admission:
    """`run_dir` is the day's run directory; `run_id` is the held run (own on admit, holder's on in-flight)."""

    state: str
    run_dir: Path
    lock_path: Path
    run_id: str = ""
    abandoned_run_id: str = ""


def _lock_dir(repo_root: Path, ceremony: str) -> Path:
    return resolve_git_common_dir(repo_root) / "coordinator-sessions" / f"{ceremony}-runs"


def _own_start_epoch() -> int:
    try:
        import psutil

        return int(psutil.Process().create_time())
    except Exception:
        return 0


def _holder_alive(holder: dict) -> bool:
    """Dead only when the liveness seam says so; any error counts as alive."""
    try:
        from coordinator_core.session import core

        if not int(holder["start_epoch"]):
            return True
        return bool(core.stable_pid_alive(holder["pid"], "", str(int(holder["start_epoch"]))))
    except Exception:
        return True


def _read_holder(lock_path: Path) -> dict | None:
    try:
        holder = json.loads(lock_path.read_text(encoding="utf-8"))
        for key in ("pid", "start_epoch", "session_id", "run_id", "day", "phase"):
            holder[key]
        if holder["phase"] not in (PHASE_LAUNCHING, PHASE_AWAITING_FINISH):
            return None
        return holder
    except Exception:
        return None


def _try_create(lock_path: Path, record: dict) -> bool:
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(record, fh)
    return True


def acquire(repo_root, *, ceremony: str, marker_path, today: str | None = None, session_id: str) -> Admission:
    """Admit one run of `ceremony`, or say why not. `today` defaults to `daily_day.local_day`."""
    root = Path(repo_root)
    day = today or local_day(str(root))
    lock_dir = _lock_dir(root, ceremony)
    run_dir = lock_dir / day
    lock_path = lock_dir / _LOCK_NAME

    marker = Path(marker_path)
    if marker.is_file() and marker.read_text(encoding="utf-8", errors="replace").strip() == day:
        return Admission(ALREADY_RAN, run_dir, lock_path)

    lock_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"{day}-{uuid.uuid4().hex[:8]}"
    record = {
        "pid": os.getpid(),
        "start_epoch": _own_start_epoch(),
        "session_id": session_id,
        "run_id": run_id,
        "day": day,
        "phase": PHASE_LAUNCHING,
    }
    abandoned = ""
    if not _try_create(lock_path, record):
        holder = _read_holder(lock_path)
        if holder is None:
            return Admission(IN_FLIGHT, run_dir, lock_path)
        if holder["phase"] == PHASE_AWAITING_FINISH:
            if holder["day"] == day:
                return Admission(IN_FLIGHT, run_dir, lock_path, run_id=holder["run_id"])
            abandoned = holder["run_id"]
        elif _holder_alive(holder):
            return Admission(IN_FLIGHT, run_dir, lock_path, run_id=holder["run_id"])
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        if not _try_create(lock_path, record):
            return Admission(IN_FLIGHT, run_dir, lock_path)
    run_dir.mkdir(parents=True, exist_ok=True)
    return Admission(ADMITTED, run_dir, lock_path, run_id=run_id, abandoned_run_id=abandoned)


def mark_awaiting_finish(admission: Admission) -> None:
    """Called by `launch` before it exits with a workflow emitted; its pid is dead until `finish`."""
    holder = _read_holder(admission.lock_path)
    if holder is None or holder["run_id"] != admission.run_id:
        return
    holder["phase"] = PHASE_AWAITING_FINISH
    tmp = admission.lock_path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(holder), encoding="utf-8", newline="\n")
    os.replace(tmp, admission.lock_path)


def release_run(lock_path, run_id: str) -> bool:
    """Release by run id; a lock held by a different run is left alone. True when removed."""
    path = Path(lock_path)
    holder = _read_holder(path)
    if holder is None or holder["run_id"] != run_id:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def release(admission: Admission) -> bool:
    """Release an admitted run's lock; a no-op for `in-flight` and `already-ran`."""
    if admission.state != ADMITTED:
        return False
    return release_run(admission.lock_path, admission.run_id)
