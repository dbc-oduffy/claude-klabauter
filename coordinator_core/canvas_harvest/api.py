"""canvas_harvest.api - the calls the op and the CLI make.

`harvest` and `probe` resolve the session id and the transcript files, lift the
canvas reads out of them and hand the result to the writers. The callee modules
below land after this one and are imported inside the functions, so a caller pays
only for the path it takes. `now` is a timezone-aware datetime handed unchanged to
every callee; None means the current UTC time.

Callee signatures (the contract rows C2 to C5 build to):

    lift.lift(transcripts: list[Path], artifact_id: str, *, reader_agent_id: str | None = None) -> model.Lifted
    record.write_record(lifted: model.Lifted, out_dir: Path, now: datetime) -> model.RecordWrite
    probe_stamp.stamp_probe(lifted: model.Lifted, out_dir: Path, session_id: str, now: datetime,
                            *, artifact_id: str | None = None) -> Path   (the harvest.json written)
    probe_stamp.stamp_unreadable(error: model.ReadError, artifact_id: str, out_dir: Path,
                                 session_id: str, now: datetime) -> Path | None   (None: no record)
    render.write_settled_skeleton(lifted: model.Lifted, out_dir: Path) -> bool
        (True when this call created SETTLED.md, False when it already existed)
    reader.dispatch(url: str) -> dict
        (keys subagent_type, description, prompt, harvest_argv)
    reader.named_reader(transcript: Path, artifact_id: str) -> str
        (the agentId; raises HarvestError(model.READER_NOT_NAMED))
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.canvas_harvest import model


def _resolve_inputs(url, session_id, transcript):
    artifact_id = model.artifact_id_from_url(url)
    if artifact_id is None:
        raise ValueError(f"not a canvas artifact url: {url!r}")
    sid = session_id
    if not sid:
        from coordinator_core.ops.session_context import resolve_current_session_id

        sid = resolve_current_session_id()
    if transcript is not None:
        transcripts = [Path(transcript)]
        if not transcripts[0].is_file():
            raise model.HarvestError(model.NO_TRANSCRIPT, str(transcript))
    else:
        if not sid:
            raise model.HarvestError(model.NO_SESSION_ID, "no session id to find a transcript with")
        from coordinator_core.group_em.human_entry import transcript_paths

        transcripts = transcript_paths(sid)
        if not transcripts:
            raise model.HarvestError(model.NO_TRANSCRIPT, f"none for session {sid}")
    return artifact_id, sid, transcripts


def _require_session(sid: Optional[str], lifted: model.Lifted) -> str:
    sid = sid or lifted.session_id
    if not sid:
        raise model.HarvestError(model.NO_SESSION_ID, "no session id for the probe")
    return sid


def _raise_unreadable(lifted, artifact_id, out_dir, sid, now):
    error = lifted.error
    if error.kind is None:
        raise model.HarvestError(model.READ_ERROR_UNCLASSIFIED, error.first_line)
    from coordinator_core.canvas_harvest import probe_stamp

    probe_stamp.stamp_unreadable(error, artifact_id, out_dir, _require_session(sid, lifted), now)
    raise model.HarvestError(model.CANVAS_UNREADABLE, error.kind)


def harvest(
    url, out_dir, *, session_id=None, transcript=None, now: Optional[datetime] = None, reader: bool = False
) -> model.RecordWrite:
    out_dir = Path(out_dir)
    now = now or datetime.now(timezone.utc)
    artifact_id, sid, transcripts = _resolve_inputs(url, session_id, transcript)
    from coordinator_core.canvas_harvest import lift

    lifted = lift.lift(transcripts, artifact_id)
    if lifted.error is not None:
        _raise_unreadable(lifted, artifact_id, out_dir, sid, now)
    from coordinator_core.canvas_harvest import record, render

    result = record.write_record(lifted, out_dir, now)
    if not render.write_settled_skeleton(lifted, out_dir):
        return result
    return model.RecordWrite(
        written=tuple(result.written) + (out_dir / render.SETTLED_NAME,), deleted=result.deleted
    )


def probe(url, out_dir, *, session_id=None, transcript=None, now: Optional[datetime] = None) -> model.RecordWrite:
    out_dir = Path(out_dir)
    now = now or datetime.now(timezone.utc)
    artifact_id, sid, transcripts = _resolve_inputs(url, session_id, transcript)
    from coordinator_core.canvas_harvest import lift

    lifted = lift.lift(transcripts, artifact_id)
    if lifted.error is not None:
        _raise_unreadable(lifted, artifact_id, out_dir, sid, now)
    from coordinator_core.canvas_harvest import probe_stamp

    path = probe_stamp.stamp_probe(lifted, out_dir, _require_session(sid, lifted), now, artifact_id=artifact_id)
    return model.RecordWrite(written=(path,), deleted=())
