"""canvas_harvest.probe_stamp - rewrite the `probe` key of an existing harvest.json and nothing else.

A stamp writes one file (harvest.json) and declares that one path, whatever the record's
size. It never counts the record, never deletes, and opens no frames/ or threads/ file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from coordinator_core.canvas_harvest import model
from coordinator_core.session import claimed_write

RECORD_NAME = "harvest.json"


def _load(out_dir: Path) -> Optional[dict]:
    """The parsed record, None when harvest.json is absent; `record-unparseable` when it is not a record."""
    path = Path(out_dir) / RECORD_NAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise model.HarvestError(model.RECORD_UNPARSEABLE, f"{RECORD_NAME}: {exc}") from exc
    if (
        not isinstance(record, dict)
        or not isinstance(record.get("artifact"), dict)
        or not isinstance(record.get("harvest"), dict)
    ):
        raise model.HarvestError(model.RECORD_UNPARSEABLE, f"{RECORD_NAME} lacks artifact or harvest")
    return record


def _require_same_artifact(record: dict, artifact_id: Optional[str]) -> None:
    held = record["artifact"].get("id")
    if not artifact_id or held != artifact_id:
        raise model.HarvestError(
            model.RECORD_ARTIFACT_MISMATCH, f"record holds {held!r}, read is {artifact_id!r}"
        )


def _require_session(session_id: Optional[str]) -> str:
    if not session_id:
        raise model.HarvestError(model.NO_SESSION_ID, "a probe needs a session id")
    return session_id


def _stamp(now) -> str:
    return now if isinstance(now, str) else model.utc_string(now)


def _write(out_dir: Path, record: dict, probe: dict) -> Path:
    record["probe"] = probe
    return claimed_write.replace_bytes(Path(out_dir) / RECORD_NAME, model.dump_json(record))


def _require_later(read_at: str, floors: list, what: str) -> None:
    moment = model._instant(read_at)
    if moment is None:
        raise model.HarvestError(model.PROBE_READS_STALE, f"{what} read_at {read_at!r} is not an instant")
    for label, floor_text in floors:
        floor = model._instant(floor_text) if isinstance(floor_text, str) else None
        if floor is None:
            raise model.HarvestError(model.PROBE_READS_STALE, f"{label} {floor_text!r} is not an instant")
        if moment <= floor:
            raise model.HarvestError(
                model.PROBE_READS_STALE, f"{what} read {read_at} is not later than {label} {floor_text}"
            )


def stamp_probe(lifted, out_dir, session_id, now, *, artifact_id: Optional[str] = None) -> Path:
    """Stamp a readable probe over the record in `out_dir`; returns the one path written.

    The artifact id compared with the record is `artifact_id` when given, else
    `lifted.artifact_read.artifact_id`; with neither, the record is refused as a mismatch.
    """
    out_dir = Path(out_dir)
    record = _load(out_dir)
    if record is None:
        raise model.HarvestError(model.NO_HARVEST_RECORD, f"no {RECORD_NAME} in {out_dir}")
    if artifact_id is None and lifted.artifact_read is not None:
        artifact_id = lifted.artifact_read.artifact_id
    _require_same_artifact(record, artifact_id)
    session_id = _require_session(session_id)
    if lifted.version is None or lifted.comments is None:
        raise model.HarvestError(model.NO_MAIN_LOOP_READ, "probe needs a version read and a comments read")

    floors = [("harvest.harvested_at", record["harvest"].get("harvested_at"))]
    prior = record.get("probe")
    if isinstance(prior, dict) and prior.get("probed_at") is not None:
        floors.append(("probe.probed_at", prior["probed_at"]))
    _require_later(lifted.version.read_at, floors, "version")
    _require_later(lifted.comments.read_at, floors, "comments")

    threads = lifted.comments.threads
    probe = {
        "probed_at": _stamp(now),
        "session_id": session_id,
        "readable": True,
        "unreadable_reason": None,
        "version": lifted.version.version,
        "thread_ids": model.thread_ids(threads),
        "latest_comment_at": model.latest_comment_at(threads),
    }
    return _write(out_dir, record, probe)


def stamp_unreadable(error, artifact_id, out_dir, session_id, now) -> Optional[Path]:
    """Mark the record unreadable; returns the path written, or None when no record exists."""
    out_dir = Path(out_dir)
    record = _load(out_dir)
    if record is None:
        return None
    _require_same_artifact(record, artifact_id)
    session_id = _require_session(session_id)
    if error.kind not in (model.NOT_FOUND, model.NEEDS_APPROVAL):
        raise model.HarvestError(model.READ_ERROR_UNCLASSIFIED, error.first_line)
    probe = {
        "probed_at": _stamp(now),
        "session_id": session_id,
        "readable": False,
        "unreadable_reason": error.kind,
        "version": "",
        "thread_ids": [],
        "latest_comment_at": None,
    }
    return _write(out_dir, record, probe)
