"""Is a warp run already working a sizing? The one liveness answer the submit hook and every
second entry to the same work read.

A run is live while its run-token record (`ops.dispatch_emit.run_token`: minted at emit,
consumed on admit (`run_token.admits`), inert after `TTL_SECONDS`) is present AND the submitting
session's transcript carries no terminal task notification for the harness task that ran it. The
token alone over-reports: a run that halts at a gate never commits, so its record outlives it.
The task is found through the submission record this module writes beside the run's briefs and
the `workflow-run-<session>-<task>.json` record the PostToolUse(Workflow) capture
(`hooks.postuse_advisory_dispatch`) persists, matched on the script path.

A token owns a sizing only when its record names it; a raw ask's run is reachable through
`live_run_for_session`. Launch records are read from the managed temp dir and the legacy
top-level dir.

Read-only except `record_submission` and the `managed_tempdir` mkdir on an unscoped lookup. No spawn. Every read failure answers "not live": a wrong
"live" refuses a legitimate fire, a wrong "not live" costs at most a duplicate run.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Iterator, Optional

from coordinator_core.ops.dispatch_emit import run_token as _run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.temp_layout import legacy_top_level_path, managed_tempdir

SUBMISSION_FILE = "submission.json"

#: A token whose launch was never captured is live only this long: past it, the Workflow call
#: that should have followed the emit is taken to have failed.
UNLAUNCHED_GRACE_SECONDS = 15 * 60

#: Transcript bytes searched for the terminal notification (the notification is appended after
#: the launch, so the tail holds it unless the session has run long past the run's end).
_TRANSCRIPT_TAIL_BYTES = 4 * 1024 * 1024

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _submission_path(repo_root: Path, run_id: str) -> Path:
    return Path(repo_root) / RUN_DIR_ROOT / run_id / SUBMISSION_FILE


def record_submission(
    repo_root: Path,
    run_id: str,
    *,
    session_id: Optional[str],
    transcript_path: Optional[str],
    script_path: str,
    sizing_rel: Optional[str],
) -> None:
    """Write the run's submission record; the liveness read joins it to the harness task."""
    path = _submission_path(repo_root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": run_id,
        "session_id": session_id or None,
        "transcript_path": transcript_path or None,
        "script_path": script_path,
        "sizing_rel": sizing_rel,
        "submitted_at": time.time(),
    }
    path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _token_records(repo_root: Path) -> Iterator[dict]:
    common = _run_token._common_dir(repo_root)
    if common is None:
        return
    directory = common / _run_token._DIR
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return
    now = time.time()
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or not isinstance(record.get("run_id"), str):
            continue
        minted = record.get("minted_at")
        if not isinstance(minted, (int, float)) or now - minted > _run_token.TTL_SECONDS:
            continue
        yield record


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _same_path(a: object, b: object) -> bool:
    if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
        return False
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _launch_dirs(tmpdir: Optional[str]) -> list[str]:
    """An explicit `tmpdir` alone; otherwise the managed dir, then the legacy top-level dir."""
    if tmpdir:
        return [tmpdir]
    managed = str(managed_tempdir())
    legacy = str(legacy_top_level_path(""))
    if os.path.normcase(os.path.normpath(legacy)) == os.path.normcase(os.path.normpath(managed)):
        return [managed]
    return [managed, legacy]


def _launched_task_ids(session_id: str, script_path: str, dirs: list[str]) -> list[str]:
    """Harness task ids this session launched `script_path` under, newest record last."""
    if not _SAFE_ID_RE.match(session_id or ""):
        return []
    prefix = f"workflow-run-{session_id}-"
    found: dict[str, float] = {}
    for tmpdir in dirs:
        try:
            names = os.listdir(tmpdir)
        except OSError:
            continue
        for name in names:
            if not (name.startswith(prefix) and name.endswith(".json")):
                continue
            task_id = name[len(prefix):-len(".json")]
            if not _SAFE_ID_RE.match(task_id):
                continue
            record = _read_json(Path(tmpdir) / name)
            if record and _same_path(record.get("scriptPath"), script_path):
                fired = record.get("fired_at")
                found[task_id] = fired if isinstance(fired, (int, float)) else 0.0
    return [task_id for task_id, _ in sorted(found.items(), key=lambda kv: (kv[1], kv[0]))]


def _tail(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _TRANSCRIPT_TAIL_BYTES))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _task_ended(transcript_tail: str, task_id: str) -> bool:
    from coordinator_core.workflow_watch.terminal import _find_terminal_record

    return _find_terminal_record(transcript_tail, task_id) is not None


def _run_ended(
    session_id: object, script_path: object, transcript_path: object, minted_at: float, dirs: list[str]
) -> bool:
    """True once the harness reported the run's task terminal, or its launch never happened."""
    if not isinstance(session_id, str) or not isinstance(script_path, str):
        return False
    task_ids = _launched_task_ids(session_id, script_path, dirs)
    if not task_ids:
        return time.time() - minted_at > UNLAUNCHED_GRACE_SECONDS
    if not isinstance(transcript_path, str) or not transcript_path:
        return False
    tail = _tail(transcript_path)
    return bool(tail) and _task_ended(tail, task_ids[-1])


def _record_live(
    root: Path, record: dict, session_id: Optional[str], transcript_path: Optional[str], dirs: list[str]
) -> bool:
    run_id = record["run_id"]
    submission = _read_json(_submission_path(root, run_id)) or {}
    run_session = submission.get("session_id") or record.get("session_id")
    transcript = submission.get("transcript_path") or (
        transcript_path if run_session and run_session == session_id else None
    )
    script = submission.get("script_path") or record.get("script_path")
    return not _run_ended(run_session, script, transcript, float(record["minted_at"]), dirs)


def live_run_for_sizing(
    repo_root: Path,
    sizing_rel: str,
    *,
    session_id: Optional[str] = None,
    transcript_path: Optional[str] = None,
    tmpdir: Optional[str] = None,
) -> Optional[str]:
    """The run id of a live run whose token names `sizing_rel`, else None.

    `session_id`/`transcript_path` are the asking session's: they stand in for a run emitted
    without a submission record (a CLI emit) when that run is this session's own."""
    root = Path(repo_root)
    rel = _run_token.normalize_sizing_rel(root, sizing_rel)
    if not rel:
        return None
    dirs = _launch_dirs(tmpdir)
    for record in _token_records(root):
        named = record.get("sizing_rel")
        if not isinstance(named, str) or _run_token.normalize_sizing_rel(root, named) != rel:
            continue
        if _record_live(root, record, session_id, transcript_path, dirs):
            return record["run_id"]
    return None


def live_run_for_session(
    repo_root: Path,
    session_id: Optional[str],
    *,
    transcript_path: Optional[str] = None,
    tmpdir: Optional[str] = None,
) -> Optional[str]:
    """The run id of a live run whose token names `session_id`, sizing-bound or raw ask."""
    if not session_id:
        return None
    root = Path(repo_root)
    dirs = _launch_dirs(tmpdir)
    for record in _token_records(root):
        if record.get("session_id") != session_id:
            continue
        if _record_live(root, record, session_id, transcript_path, dirs):
            return record["run_id"]
    return None
