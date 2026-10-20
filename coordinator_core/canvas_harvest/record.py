"""canvas_harvest.record - writes the harvest record directory from a lifted canvas read.

Every check and every frame read happens before the first write, so a refusal leaves a
prior record whole; harvest.json is written last so a record is never visible half-written.
Every write is claimed_write.replace_bytes. A removed file cannot be claimed and is returned
in RecordWrite.deleted for the committing session to name.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from coordinator_core.canvas_harvest import model
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.session.claimed_write import replace_bytes

_EXTRA_FILES = 3  # threads.rendered.txt, SETTLED.md, harvest.json


def _check_prior_record(out_dir: Path, artifact_id: str) -> None:
    prior = out_dir / "harvest.json"
    if not prior.is_file():
        return
    try:
        prior_id = json.loads(prior.read_text(encoding="utf-8"))["artifact"]["id"]
    except (OSError, ValueError, KeyError, TypeError):
        raise model.HarvestError(model.RECORD_UNPARSEABLE, str(prior.name)) from None
    if prior_id != artifact_id:
        raise model.HarvestError(
            model.RECORD_ARTIFACT_MISMATCH, f"record holds {prior_id!r}, harvest is of {artifact_id!r}"
        )


def _frame_source(frame: model.FrameSource, transcript_dir) -> Path:
    source = Path(frame.saved_to)
    if source.is_absolute():
        return source
    if transcript_dir is None:
        raise model.HarvestError(model.FRAME_SOURCE_MISSING, f"{frame.saved_to!r} has no transcript directory")
    base = Path(transcript_dir)
    resolved = contained_path(base / source, [base])
    if resolved is None:
        raise model.HarvestError(model.UNSAFE_PATH, f"{frame.saved_to!r} resolves outside the transcript directory")
    return base / source


def _stale(directory: Path, keep: set, pattern: str) -> list:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob(pattern) if p.is_file() and p not in keep)


def write_record(lifted: model.Lifted, out_dir, now: datetime, *, account: str | None = None) -> model.RecordWrite:
    """Write frames, threads, threads.rendered.txt and harvest.json under `out_dir`; return what was written and removed.

    `account` is the login the harvest ran under; harvest.json records it as `harvest.account` (null when None).
    """
    out_dir = Path(out_dir)
    read = lifted.artifact_read
    if read is None:
        raise model.HarvestError(model.NO_MAIN_LOOP_READ, "no artifact read to record")
    comments = lifted.comments
    threads = list(comments.threads) if comments is not None else []
    rendered = comments.rendered_text if comments is not None else ""
    frames = list(read.frames)

    # Ceiling counts project/** only; lift drops type runtime files, so frames holds none in production.
    count = sum(model.is_project_file(f.path) for f in frames) + len(threads) + _EXTRA_FILES
    if count > model.MAX_RECORD_FILES:
        raise model.HarvestError(model.RECORD_TOO_LARGE, f"{count} files, ceiling {model.MAX_RECORD_FILES}")

    frames_dir = out_dir / "frames"
    threads_dir = out_dir / "threads"
    frame_targets = [model.safe_child(frames_dir, f.path) for f in frames]
    ids = model.thread_ids(threads)
    thread_targets = [model.safe_child(threads_dir, f"{tid}.json") for tid in ids]
    for names, what in (([f.path for f in frames], "frame path"), ([f"{t}.json" for t in ids], "thread id")):
        folded = [n.casefold() for n in names]
        if len(set(folded)) != len(folded):
            raise model.HarvestError(model.UNSAFE_PATH, f"two {what}s equal under case-folding")

    _check_prior_record(out_dir, read.artifact_id)

    payloads = []
    for frame in frames:
        source = _frame_source(frame, lifted.transcript_dir)
        try:
            data = source.read_bytes()
        except OSError:
            raise model.HarvestError(model.FRAME_SOURCE_MISSING, str(frame.saved_to)) from None
        if hashlib.sha256(data).hexdigest() != frame.sha256:
            raise model.HarvestError(model.FRAME_SHA_MISMATCH, frame.path)
        payloads.append(data)

    harvest = {
        "schema": model.SCHEMA,
        "artifact": {
            "url": f"claude.ai/artifact/{read.artifact_id}",
            "id": read.artifact_id,
            "title": read.title,
            "type": read.type,
        },
        "harvest": {
            "harvested_at": model.utc_string(now),
            "account": account,
            "version": read.version,
            "thread_ids": ids,
            "latest_comment_at": model.latest_comment_at(threads),
            "frames": [{"path": f.path, "sha256": f.sha256} for f in frames],
        },
    }

    written = []
    for target, data in zip(frame_targets, payloads):
        written.append(replace_bytes(target, data))
    for target, thread in zip(thread_targets, threads):
        written.append(replace_bytes(target, model.dump_json(thread)))
    written.append(replace_bytes(out_dir / "threads.rendered.txt", rendered.encode("utf-8")))

    deleted = _stale(frames_dir, set(frame_targets), "**/*") + _stale(threads_dir, set(thread_targets), "*.json")
    for path in deleted:
        path.unlink()

    written.append(replace_bytes(out_dir / "harvest.json", model.dump_json(harvest)))
    return model.RecordWrite(written=tuple(written), deleted=tuple(deleted))
