"""Is a warp run already working a sizing? The one liveness answer the submit hook and every
second entry to the same work read.

A run is live while its run-token record (`ops.dispatch_emit.run_token`: minted at emit,
redeemed when the in-run commit is attempted, inert after `TTL_SECONDS`) is present AND the submitting
session's transcript carries no terminal task notification for the harness task that ran it. The
token alone over-reports: a run that halts at a gate never commits, so its record outlives it.
The task is found through the submission record this module writes beside the run's briefs and
the `workflow-run-<session>-<task>.json` record the PostToolUse(Workflow) capture
(`hooks.postuse_advisory_dispatch`) persists, matched on the script path.

A run whose submitting call was refused (no launch record and a lone non-launch `tool_result`
for the stored `tool_use_id`) has ended too, for every reader and every session.

A token owns a sizing only when its record names it: from the emit (a `--sizing` run) or from the
gate's `bind_sizing` (a raw ask). A raw ask's run is also reachable through `live_run_for_session`.
`unlaunched_run_for_sizing` names this session's token whose submitting call was refused (a
non-launch result for the stored `tool_use_id`). Launch records are read from the managed temp dir
and the legacy top-level dir.

Read-only except `record_submission`, the `managed_tempdir` mkdir on an unscoped lookup, and
`run_token.consume` of a run that ended on evidence (task terminal, halted returns included, or a
refused submitting call); the unlaunched-grace timer answers "not live" but never consumes. No spawn. Every read failure answers "not live": a wrong
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

#: Transcript bytes before the tail window searched, by the run's task id, when the tail holds no
#: terminal notification: bounds the read of a session that ran long past the run's end.
_TRANSCRIPT_HEAD_BYTES = 64 * 1024 * 1024

_EVIDENCE = "evidence"
_TIMER = "timer"

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
    tool_use_id: Optional[str] = None,
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
        "tool_use_id": tool_use_id or None,
        "submitted_at": time.time(),
    }
    path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _token_records(repo_root: Path) -> Iterator[dict]:
    yield from _run_token.live_records(repo_root)


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
    session_id: object,
    script_path: object,
    transcript_path: object,
    minted_at: float,
    dirs: list[str],
    tool_use_id: object = None,
) -> Optional[str]:
    """Why the run is over, else None while it is live: `_EVIDENCE` once the harness reported the
    run's task terminal (halted returns included) or the submitting call was refused; `_TIMER`
    once the unlaunched grace has passed, which is not evidence and never spends the token."""
    if not isinstance(session_id, str) or not isinstance(script_path, str):
        return None
    task_ids = _launched_task_ids(session_id, script_path, dirs)
    has_transcript = isinstance(transcript_path, str) and bool(transcript_path)
    tail = _tail(transcript_path) if has_transcript else ""
    if not task_ids:
        if tail and isinstance(tool_use_id, str) and tool_use_id:
            seen = tail if tool_use_id in tail else _earlier_lines(transcript_path, tool_use_id) + "\n" + tail
            if _call_refused(seen, tool_use_id):
                return _EVIDENCE
            recovered = _launch_task_id(seen, tool_use_id)
            if recovered:
                task_ids = [recovered]
        if not task_ids:
            return _TIMER if time.time() - minted_at > UNLAUNCHED_GRACE_SECONDS else None
    if not tail:
        return None
    task_id = task_ids[-1]
    if _task_ended(tail, task_id) or _task_gone(tail, task_id):
        return _EVIDENCE
    earlier = _earlier_lines(transcript_path, task_id)
    return _EVIDENCE if earlier and (_task_ended(earlier, task_id) or _task_gone(earlier, task_id)) else None


def _earlier_lines(path: str, needle: str) -> str:
    """The lines holding `needle` in the `_TRANSCRIPT_HEAD_BYTES` before the tail window: a run
    that ended in a session that kept going has its launch and terminal notification there.
    Reads and holds up to `_TRANSCRIPT_HEAD_BYTES` per call (unmeasured against the 500ms bar); a
    line straddling the tail boundary is split across both windows and matches neither."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            stop = max(0, handle.tell() - _TRANSCRIPT_TAIL_BYTES)
            start = max(0, stop - _TRANSCRIPT_HEAD_BYTES)
            handle.seek(start)
            data = handle.read(stop - start)
    except OSError:
        return ""
    key = needle.encode("utf-8")
    lines: list[bytes] = []
    pos = data.find(key)
    while pos != -1:
        begin = data.rfind(b"\n", 0, pos) + 1
        end = data.find(b"\n", pos)
        if end == -1:
            end = len(data)
        lines.append(data[begin:end])
        pos = data.find(key, end)
    return b"\n".join(lines).decode("utf-8", errors="replace")


def _task_gone(transcript_tail: str, task_id: str) -> bool:
    """True when a tail line reports `No task found` for `task_id`: the harness never ran it, or
    reaped it, so no terminal notification can follow."""
    if task_id not in transcript_tail:
        return False
    return any("No task found" in ln and task_id in ln for ln in transcript_tail.split("\n"))


def _launch_task_id(transcript_tail: str, tool_use_id: str) -> Optional[str]:
    """The harness task id of the lone `async_launched` result for `tool_use_id` in the tail, for
    a run whose `workflow-run-*` launch record temp cleanup removed."""
    if tool_use_id not in transcript_tail:
        return None
    for line in transcript_tail.split("\n"):
        if tool_use_id not in line or '"tool_result"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        message = obj.get("message") if isinstance(obj, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        if len(blocks) != 1 or blocks[0].get("tool_use_id") != tool_use_id:
            continue
        launch = obj.get("toolUseResult")
        if isinstance(launch, dict) and launch.get("status") == "async_launched":
            task_id = launch.get("taskId")
            if isinstance(task_id, str) and _SAFE_ID_RE.match(task_id):
                return task_id
        return None
    return None


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
    ended = _run_ended(
        run_session, script, transcript, float(record["minted_at"]), dirs, submission.get("tool_use_id")
    )
    if ended == _EVIDENCE:
        _run_token.consume(root, run_id)
    return ended is None


def unsized_run(repo_root: Path, session_id: Optional[str]) -> Optional[str]:
    """The run id of this session's live raw-ask run whose gate has not bound a sizing, when it is
    the session's only live run, else None.

    Such a run is in its size stage, where no product file is written. With a second live run in
    the session a writer cannot be attributed to either, and a wrong answer halts the other run's
    executors, so none is named."""
    if not session_id:
        return None
    root = Path(repo_root)
    dirs = _launch_dirs(None)
    live: list[tuple[str, bool]] = []
    for record in _token_records(root):
        if record.get("session_id") != session_id:
            continue
        run_id = record["run_id"]
        submission = _read_json(_submission_path(root, run_id)) or {}
        script = submission.get("script_path") or record.get("script_path")
        transcript = submission.get("transcript_path")
        if _run_ended(
            session_id, script, transcript, float(record["minted_at"]), dirs, submission.get("tool_use_id")
        ) is not None:
            continue
        unsized = record.get("sizing_rel") is None and not submission.get("sizing_rel")
        live.append((run_id, unsized))
    if len(live) == 1 and live[0][1]:
        return live[0][0]
    return None


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


def _call_refused(transcript_tail: str, tool_use_id: str) -> bool:
    """True only when the transcript holds a lone `tool_result` for `tool_use_id` that is not an
    `async_launched` launch. A call with no result yet, a launch, or a result line carrying
    several results (its `toolUseResult` is unattributable) is not refused."""
    if tool_use_id not in transcript_tail:
        return False
    for line in transcript_tail.split("\n"):
        if tool_use_id not in line or '"tool_result"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        message = obj.get("message") if isinstance(obj, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        if len(blocks) != 1 or blocks[0].get("tool_use_id") != tool_use_id:
            continue
        launch = obj.get("toolUseResult")
        launched = isinstance(launch, dict) and launch.get("status") == "async_launched"
        return not launched
    return False


def unlaunched_run_for_sizing(
    repo_root: Path,
    sizing_rel: str,
    session_id: Optional[str],
    *,
    tmpdir: Optional[str] = None,
) -> Optional[str]:
    """The run id of this session's token for `sizing_rel` whose submitting call was refused.

    Superseded only on positive evidence: no joined launch record AND the submission's
    `tool_use_id` has a non-launch `tool_result` in the transcript. A call still in flight (no
    result yet), a call absent from the transcript, a submission that stored no id, and a token
    of another session all read "not unlaunched"; the grace period retires the rest."""
    if not session_id:
        return None
    root = Path(repo_root)
    rel = _run_token.normalize_sizing_rel(root, sizing_rel)
    if not rel:
        return None
    dirs = _launch_dirs(tmpdir)
    for record in _token_records(root):
        named = record.get("sizing_rel")
        if not isinstance(named, str) or _run_token.normalize_sizing_rel(root, named) != rel:
            continue
        submission = _read_json(_submission_path(root, record["run_id"])) or {}
        if (submission.get("session_id") or record.get("session_id")) != session_id:
            continue
        script = submission.get("script_path") or record.get("script_path")
        if isinstance(script, str) and _launched_task_ids(session_id, script, dirs):
            continue
        tool_use_id = submission.get("tool_use_id")
        transcript = submission.get("transcript_path")
        if not isinstance(tool_use_id, str) or not tool_use_id or not isinstance(transcript, str):
            continue
        tail = _tail(transcript)
        if tail and _call_refused(tail, tool_use_id):
            return record["run_id"]
    return None
