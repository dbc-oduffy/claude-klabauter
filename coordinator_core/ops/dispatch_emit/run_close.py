"""
coordinator_core.ops.dispatch_emit.run_close -- the run-close leg vocabulary, close-block
builder and close-job record.

Purpose: the shared interface of ``dispatch.terminal_commit``'s ``close`` block. The inline
leg modules and ``close.background`` both speak ``LegResult``; the close-job record is how a
queued leg's outcome reaches the reader.

Negative-spec:
  - No git, no subprocess, no heavy import: terminal_commit has an 80ms cold-import bar.
  - A run-bound leg nobody reported is ``failed`` ("leg did not run"), never absent.
  - A ``queued`` leg is not ``outstanding`` at reply time; ``read_close_job`` is what turns
    an unfinished one into ``failed``.
  - No detail or record carries an absolute, drive-lettered or backslashed path.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

RUN_BOUND_LEGS: tuple[str, ...] = (
    "review",
    "plan-reconciliation",
    "lessons",
    "completion-entry",
    "baton-ship",
    "baton-drain",
    "orientation-refresh",
    "memo-lifecycle",
    "post-close",
)
SESSION_BOUND_LEGS: tuple[str, ...] = ("session-shape", "session-residue")
STATUSES: frozenset[str] = frozenset(
    {"ran", "discharged", "not-applicable", "queued", "failed", "session-bound"}
)

_DRIVE = re.compile(r"^[A-Za-z]:")
_TOKEN_EDGES = "\"'()[]{}<>,;`"


def _unhygienic_token(text: str) -> Optional[str]:
    for raw in text.split():
        token = raw.strip(_TOKEN_EDGES)
        if "\\" in token or token.startswith("/") or _DRIVE.match(token):
            return token
    return None


def check_path_hygiene(detail: str) -> None:
    """Raise ValueError when *detail* carries an absolute, drive-lettered or backslashed path."""
    bad = _unhygienic_token(detail)
    if bad is not None:
        raise ValueError(f"leg detail carries a non-repo-relative path: {bad!r}")


@dataclass(frozen=True)
class LegResult:
    """One leg's outcome; ``detail`` is refused if it names a non-repo-relative path."""

    status: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown leg status: {self.status!r}")
        check_path_hygiene(self.detail)

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status, "detail": self.detail}


def build_close_block(
    run_id: str,
    applies: bool,
    legs: Mapping[str, LegResult],
    completion_entry: Any = None,
) -> dict[str, Any]:
    """Build ``{applies, run_id, legs, outstanding, completion_entry}``.

    ``outstanding`` is every run-bound leg whose status is ``failed``. With ``applies``
    false every run-bound leg is ``not-applicable``.
    """
    check_path_hygiene(run_id)
    out: dict[str, dict[str, str]] = {}
    for leg in RUN_BOUND_LEGS:
        if not applies:
            result = LegResult("not-applicable", "run does not land code")
        else:
            result = legs.get(leg) or LegResult("failed", "leg did not run")
        out[leg] = result.to_dict()
    for leg in SESSION_BOUND_LEGS:
        out[leg] = LegResult("session-bound").to_dict()
    outstanding = [leg for leg in RUN_BOUND_LEGS if out[leg]["status"] == "failed"]
    return {
        "applies": applies,
        "run_id": run_id,
        "legs": out,
        "outstanding": outstanding,
        "completion_entry": completion_entry,
    }


def close_job_path(run_id: str) -> str:
    """Repo-relative forward-slash path of the run's close-job record."""
    return f".coordinator-local/close-jobs/{run_id}.json"


def _job_file(root: Path, run_id: str) -> Path:
    return Path(root) / close_job_path(run_id)


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def write_close_job(
    root: Path, run_id: str, queued_legs: Iterable[str], deadline: datetime
) -> str:
    """Write the record naming *queued_legs* and *deadline*; return its repo-relative path."""
    legs = {leg: {"status": "queued", "detail": ""} for leg in queued_legs}
    for leg in legs:
        if leg not in RUN_BOUND_LEGS:
            raise ValueError(f"not a run-bound leg: {leg!r}")
    _write_atomic(
        _job_file(root, run_id),
        {"run_id": run_id, "deadline": deadline.isoformat(), "legs": legs},
    )
    return close_job_path(run_id)


def _load(root: Path, run_id: str) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(_job_file(root, run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("legs"), dict) else None


def record_leg_outcome(
    root: Path, run_id: str, leg: str, status: str, detail: str = ""
) -> None:
    """Record *leg*'s outcome in the run's close-job record (the job's only write)."""
    result = LegResult(status, detail)
    record = _load(root, run_id)
    if record is None:
        raise FileNotFoundError(close_job_path(run_id))
    if leg not in record["legs"]:
        raise ValueError(f"leg not queued in {close_job_path(run_id)}: {leg!r}")
    record["legs"][leg] = result.to_dict()
    _write_atomic(_job_file(root, run_id), record)


def read_close_job(
    root: Path,
    run_id: str,
    now: datetime,
    legs: Iterable[str] = (),
) -> dict[str, LegResult]:
    """Resolve each queued leg to its recorded outcome.

    A leg is ``failed`` when the record is absent (*legs* names the legs owed then) or
    the deadline has passed with the leg still unrecorded; otherwise it stays ``queued``.
    """
    record = _load(root, run_id)
    if record is None:
        return {leg: LegResult("failed", "job record missing") for leg in legs}
    deadline_text = str(record.get("deadline", ""))
    try:
        late = now > datetime.fromisoformat(deadline_text)
    except ValueError:
        late = True
    resolved: dict[str, LegResult] = {}
    for leg, entry in record["legs"].items():
        status = entry.get("status") if isinstance(entry, dict) else None
        if status in STATUSES and status != "queued":
            resolved[leg] = LegResult(status, str(entry.get("detail", "")))
        elif late:
            resolved[leg] = LegResult("failed", f"job not finished by {deadline_text}")
        else:
            resolved[leg] = LegResult("queued", f"job record {close_job_path(run_id)}")
    return resolved
