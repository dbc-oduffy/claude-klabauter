"""canvas_harvest.probe_stamp - rewrite the `probe` key of an existing harvest.json and nothing else.

A stamp writes one file (harvest.json) and declares that one path, whatever the record's
size. It never counts the record, never deletes, and opens no frames/ or threads/ file.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Optional

from coordinator_core.canvas_harvest import account, model
from coordinator_core.session import claimed_write
from coordinator_core.timestamps import with_age

RECORD_NAME = "harvest.json"
OTHER_ACCOUNT = "other-account"
_GIT_TIMEOUT_S = 5


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
        raise model.HarvestError(model.PROBE_READS_STALE, f"{what} read_at {with_age(read_at)} is not an instant")
    for label, floor_text in floors:
        floor = model._instant(floor_text) if isinstance(floor_text, str) else None
        if floor is None:
            raise model.HarvestError(model.PROBE_READS_STALE, f"{label} {floor_text!r} is not an instant")
        if moment <= floor:
            raise model.HarvestError(
                model.PROBE_READS_STALE, f"{what} read {with_age(read_at)} is not later than {label} {floor_text}"
            )


def _git(out_dir: Path, *args: str) -> bytes:
    done = subprocess.run(
        ["git", "-C", str(out_dir), *args],
        capture_output=True,
        timeout=_GIT_TIMEOUT_S,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return done.stdout


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _without_probe(raw: bytes) -> dict:
    record = json.loads(raw.decode("utf-8"))
    record.pop("probe", None)
    return record


def record_committed(out_dir, record: dict) -> bool:
    """True when the record's SETTLED.md, listed frames and threads equal their HEAD blobs and harvest.json
    at HEAD equals the disk record with `probe` stripped from both. Read-only git; False on any failure.
    Compares raw disk bytes to SHA-1 blob ids, so a checkout that rewrites line endings (autocrlf) or a
    sha256 object-format repo reads False: the failure direction is "not committed", never a false True.
    """
    out_dir = Path(out_dir)
    try:
        harvest = record["harvest"]
        listed = ["SETTLED.md"]
        listed += [f"frames/{frame['path']}" for frame in harvest["frames"]]
        listed += [f"threads/{thread_id}.json" for thread_id in harvest["thread_ids"]]
        tree = _git(out_dir, "ls-tree", "-r", "-z", "HEAD", "--", ".")
        blobs = {}
        for entry in tree.split(b"\0"):
            if entry:
                meta, _, name = entry.partition(b"\t")
                blobs[name.decode("utf-8")] = meta.split()[2].decode("ascii")
        for rel in listed:
            if blobs.get(rel) != _blob_sha((out_dir / rel).read_bytes()):
                return False
        head = _git(out_dir, "show", f"HEAD:./{RECORD_NAME}")
        disk = (out_dir / RECORD_NAME).read_bytes()
        return _without_probe(head) == _without_probe(disk)
    except Exception:
        return False


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
        "account": account.current_login(),
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
    login = account.current_login()
    held = record["harvest"].get("account")
    reason = error.kind
    if reason == model.NOT_FOUND and login and held and login != held:
        reason = OTHER_ACCOUNT
    probe = {
        "probed_at": _stamp(now),
        "session_id": session_id,
        "account": login,
        "readable": False,
        "unreadable_reason": reason,
        "record_committed": record_committed(out_dir, record),
        "version": "",
        "thread_ids": [],
        "latest_comment_at": None,
    }
    return _write(out_dir, record, probe)
