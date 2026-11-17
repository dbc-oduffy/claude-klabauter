"""Job record for the job-cancel substrate: schema, settings-home path, atomic write/read/delete.

A record is `<settings home>/jobs/<job_id>.json`; `root_ctime` pins the root pid so a reused
pid is never mistaken for the job. Spawn-free by contract: nothing here imports `subprocess`.
Contract for peers: docs/reference/job-cancel-contract.md.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.ops._path_guard import safe_id

SCHEMA = "job-record/1"


class JobRecordError(ValueError):
    """Raised for an unsafe job id or a malformed record."""


@dataclass(frozen=True)
class JobRecord:
    """One job's registration: tree root (pid, ctime), lock and staging paths, ownership."""

    job_id: str
    root_pid: int
    root_ctime: float
    lock_path: Optional[str]
    staging_path: Optional[str]
    owner_repo: str
    started_at: str
    command: Optional[str] = None
    schema: str = SCHEMA


def record_path(job_id: str) -> Path:
    """Return `<settings home>/jobs/<job_id>.json`; raise JobRecordError if `job_id` is not a safe id."""
    if not isinstance(job_id, str) or not safe_id(job_id):
        raise JobRecordError(f"unsafe job_id: {job_id!r}")
    return settings_home() / "jobs" / f"{job_id}.json"


def write_record(record: JobRecord) -> Path:
    """Atomically write `record` (tmp + os.replace) and return its path."""
    path = record_path(record.job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(asdict(record), indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return path


def read_record(job_id: str) -> Optional[JobRecord]:
    """Return the record for `job_id`, or None when absent; raise JobRecordError if malformed."""
    path = record_path(job_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise JobRecordError(f"unreadable job record {path.name}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise JobRecordError(f"job record {path.name}: schema is not {SCHEMA}")
    try:
        return JobRecord(**raw)
    except TypeError as exc:
        raise JobRecordError(f"job record {path.name}: {exc}") from exc


def delete_record(job_id: str) -> bool:
    """Delete the record; return False when it was already absent."""
    try:
        record_path(job_id).unlink()
    except FileNotFoundError:
        return False
    return True


def _norm(p: Path) -> str:
    return os.path.normcase(os.path.realpath(p))


def _is_within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def staging_is_removable(path: str, repo_root: Path) -> Optional[str]:
    """Return a refusal reason, or None when `path` may be removed as a job's staging dir.

    Refuses a relative path, a filesystem/drive root, the user home, the settings home, and
    the repo root or any ancestor of it.
    """
    if not path or not os.path.isabs(path):
        return "staging path is not absolute"
    target = _norm(Path(path))
    if Path(target).parent == Path(target):
        return "staging path is a filesystem root"
    if target == _norm(Path.home()):
        return "staging path is the user home"
    if target == _norm(settings_home()):
        return "staging path is the settings home"
    if _is_within(_norm(repo_root), target):
        return "staging path is the repo root or an ancestor of it"
    return None
