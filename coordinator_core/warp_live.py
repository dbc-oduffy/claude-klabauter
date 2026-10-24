"""Is a warp run already working a sizing? The one liveness answer the submit hook and every
second entry to the same work read.

A run is live while its run-token record (`ops.dispatch_emit.run_token`: minted at emit,
redeemed when the in-run commit is attempted, inert after `TTL_SECONDS`) is present AND the submitting
session's transcript carries no terminal task notification for the harness task that ran it. The
token alone over-reports: a run that halts at a gate never commits, so its record outlives it.
The task is found through the submission record this module writes beside the run's briefs and
the `workflow-run-<session>-<task>.json` record the PostToolUse(Workflow) capture
(`hooks.postuse_advisory_dispatch`) persists, matched on the script path.

An ownerless token (no session id, or no script path, on its record or submission) has no launch to
join and no transcript to read, so it reads ended on the unlaunched grace alone and is never spent.

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
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, record_dir as _ask_record_dir
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import STATUS_HALTED as _STATUS_HALTED
from coordinator_core.ops.dispatch_emit.halted_run import (
    HALTED_FILE as _HALTED_FILE, STATUS_LANDED as _STATUS_LANDED,
)
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

#: Bytes read per step of the head-window scan.
_SCAN_CHUNK_BYTES = 1024 * 1024

_EVIDENCE = "evidence"
_TIMER = "timer"

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _submission_path(repo_root: Path, run_id: str) -> Path:
    return Path(repo_root) / RUN_DIR_ROOT / run_id / SUBMISSION_FILE


def _head_sha_or_none(repo_root: Path) -> Optional[str]:
    try:
        from coordinator_core.git.git_state import head_sha

        return head_sha(repo_root)
    except Exception:
        return None


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
    """Write the run's submission record; the liveness read joins it to the harness task.

    `run_base_sha` is HEAD at submit (an in-process ref read, no spawn); null when HEAD is unreadable.
    """
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
        "run_base_sha": _head_sha_or_none(repo_root),
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
    return [task_id for task_id, _ in _launch_records(session_id, script_path, dirs)]


class LaunchSnapshot:
    """One session's launch-record filenames per launch dir, listed once, with each record read at
    most once. Scoped to a single caller's loop: it never outlives it, so nothing invalidates it."""

    def __init__(self, session_id: str, dirs: list[str]) -> None:
        self.session_id = session_id
        self.dirs = list(dirs)
        self._names: dict[str, list[str]] = {}
        self._records: dict[str, Optional[dict]] = {}
        self._tails: dict[str, str] = {}
        if _SAFE_ID_RE.match(session_id or ""):
            prefix = f"workflow-run-{session_id}-"
            for tmpdir in self.dirs:
                try:
                    listed = os.listdir(tmpdir)
                except OSError:
                    continue
                self._names[tmpdir] = [n for n in listed if n.startswith(prefix) and n.endswith(".json")]

    def tail(self, path: str) -> str:
        if path not in self._tails:
            self._tails[path] = _tail(path)
        return self._tails[path]

    def names(self, tmpdir: str) -> list[str]:
        return self._names.get(tmpdir, [])

    def read(self, tmpdir: str, name: str) -> Optional[dict]:
        key = os.path.join(tmpdir, name)
        if key not in self._records:
            self._records[key] = _read_json(Path(tmpdir) / name)
        return self._records[key]


def _launch_records(
    session_id: str, script_path: str, dirs: list[str], snapshot: Optional[LaunchSnapshot] = None
) -> list[tuple[str, dict]]:
    """(task id, launch record) pairs for `script_path` in this session, newest record last."""
    if not _SAFE_ID_RE.match(session_id or ""):
        return []
    if snapshot is not None and (snapshot.session_id != session_id or snapshot.dirs != list(dirs)):
        snapshot = None
    if snapshot is None:
        snapshot = LaunchSnapshot(session_id, dirs)
    prefix = f"workflow-run-{session_id}-"
    found: dict[str, tuple[float, dict]] = {}
    for tmpdir in dirs:
        for name in snapshot.names(tmpdir):
            task_id = name[len(prefix):-len(".json")]
            if not _SAFE_ID_RE.match(task_id):
                continue
            record = snapshot.read(tmpdir, name)
            if record and _same_path(record.get("scriptPath"), script_path):
                fired = record.get("fired_at")
                found[task_id] = (fired if isinstance(fired, (int, float)) else 0.0, record)
    ordered = sorted(found.items(), key=lambda kv: (kv[1][0], kv[0]))
    return [(task_id, rec) for task_id, (_, rec) in ordered]


def _own_launch_ids(
    session_id: str,
    script_path: str,
    dirs: list[str],
    tool_use_id: object,
    minted_at: float,
    snapshot: Optional[LaunchSnapshot] = None,
) -> list[str]:
    """Task ids of the launches this mint submitted. A halted run re-fired under its own id shares
    the script path with its earlier launches, so the path alone never attributes a launch.

    A pending record (its filename suffix is a tool_use_id, not a task id) is never a launch. With
    a submitted `tool_use_id`, a record naming another one is an earlier launch; a record
    naming none (every capture names one) cannot be disproved and stays. Without a submitted id, a
    named record fired before the mint's whole second is an earlier launch."""
    own: list[str] = []
    for task_id, record in _launch_records(session_id, script_path, dirs, snapshot):
        if record.get("pending"):
            continue
        recorded = record.get("tool_use_id")
        if not (isinstance(recorded, str) and recorded):
            own.append(task_id)
            continue
        if isinstance(tool_use_id, str) and tool_use_id:
            if recorded != tool_use_id:
                continue
        else:
            fired = record.get("fired_at")
            if not isinstance(fired, (int, float)) or fired < int(minted_at):
                continue
        own.append(task_id)
    return own


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
    snapshot: Optional[LaunchSnapshot] = None,
) -> Optional[str]:
    """Why the run is over, else None while it is live: `_EVIDENCE` once the harness reported the
    run's task terminal (halted returns included) or the submitting call was refused; `_TIMER`
    once the unlaunched grace has passed, which is not evidence and never spends the token."""
    if not (isinstance(session_id, str) and session_id) or not isinstance(script_path, str):
        # An ownerless token is the unlaunched state with no owner to read: it ages out on the
        # same bound and is never spent.
        return _TIMER if time.time() - minted_at > UNLAUNCHED_GRACE_SECONDS else None
    task_ids = _own_launch_ids(session_id, script_path, dirs, tool_use_id, minted_at, snapshot)
    has_transcript = isinstance(transcript_path, str) and bool(transcript_path)
    if not has_transcript:
        tail = ""
    else:
        tail = snapshot.tail(transcript_path) if snapshot is not None else _tail(transcript_path)
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
    Streams the window in `_SCAN_CHUNK_BYTES` reads and keeps only matching lines plus the open
    partial line, so memory is bounded by the chunk and the longest line, never the window (a
    window with no newline at all degrades to holding it, re-copied per chunk); a
    line straddling the tail boundary is split across both windows and matches neither."""
    key = needle.encode("utf-8")
    lines: list[bytes] = []
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            stop = max(0, handle.tell() - _TRANSCRIPT_TAIL_BYTES)
            start = max(0, stop - _TRANSCRIPT_HEAD_BYTES)
            handle.seek(start)
            remaining = stop - start
            carry = b""
            while remaining > 0:
                chunk = handle.read(min(_SCAN_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                data = carry + chunk
                cut = data.rfind(b"\n") + 1
                carry = data[cut:]
                if cut and data.find(key, 0, cut) != -1:
                    # rstrip(b"\r"): a CRLF transcript's lines match the LF-split tail's.
                    lines.extend(ln.rstrip(b"\r") for ln in data[:cut].split(b"\n") if key in ln)
            if key in carry:
                lines.append(carry.rstrip(b"\r"))
    except OSError:
        return ""
    return b"\n".join(lines).decode("utf-8", errors="replace")


def _task_gone(transcript_tail: str, task_id: str) -> bool:
    """True when the tail holds the harness's `No task found with ID: <task_id>` result: the harness
    never ran it, or reaped it, so no terminal notification can follow. Trap: prose that quotes
    `No task found` beside the id (a peer's ask, a log excerpt) must not spend a live token."""
    return f"No task found with ID: {task_id}" in transcript_tail


def _launch_task_id(transcript_tail: str, tool_use_id: str) -> Optional[str]:
    """The harness task id of the lone `async_launched` result for `tool_use_id` in the tail, for
    a run whose `workflow-run-*` launch record temp cleanup removed."""
    launch = _launch_result(transcript_tail, tool_use_id)
    task_id = launch.get("taskId") if launch else None
    return task_id if isinstance(task_id, str) and _SAFE_ID_RE.match(task_id) else None


def _launch_result(transcript_tail: str, tool_use_id: str) -> Optional[dict]:
    """The `toolUseResult` dict of the lone `async_launched` tool_result for `tool_use_id`."""
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
            return launch
        return None
    return None


def _run_transcript_dir(
    session_id: str,
    script_path: object,
    transcript_path: object,
    tool_use_id: object,
    dirs: list[str],
) -> Optional[str]:
    """Where this run's workflow writes its agents' transcripts: the newest launch record's
    `transcript_dir`, else the submitting call's launch result `transcriptDir`; None when neither
    is recoverable (unknown attribution), never raises."""
    try:
        if isinstance(script_path, str) and script_path:
            for _, record in reversed(_launch_records(session_id, script_path, dirs)):
                found = record.get("transcript_dir")
                if isinstance(found, str) and found:
                    return found
        if isinstance(transcript_path, str) and transcript_path and isinstance(tool_use_id, str) and tool_use_id:
            tail = _tail(transcript_path)
            seen = tail if tool_use_id in tail else _earlier_lines(transcript_path, tool_use_id) + "\n" + tail
            launch = _launch_result(seen, tool_use_id)
            found = launch.get("transcriptDir") if launch else None
            if isinstance(found, str) and found:
                return found
    except Exception:
        return None
    return None


def size_stage_run(
    repo_root: Path, session_id: Optional[str], spawn_dir: Optional[str]
) -> Optional[str]:
    """The run id of a live raw-ask run (token kind "ask"; a grind token never names) of this
    session that has bound no sizing and whose launch
    `transcript_dir` is `spawn_dir`: the writer is one of that run's own spawns, in its size stage.

    Unknown attribution (no session, no spawn dir, no recoverable transcript dir) answers None,
    so an agent outside the run is never named."""
    if not session_id or not spawn_dir:
        return None
    root = Path(repo_root)
    dirs = _launch_dirs(None)
    for record in _token_records(root):
        if record.get("session_id") != session_id or record.get("kind", "ask") != "ask":
            continue
        run_id = record["run_id"]
        submission = _read_json(_submission_path(root, run_id)) or {}
        if record.get("sizing_rel") is not None or submission.get("sizing_rel"):
            continue
        script = submission.get("script_path") or record.get("script_path")
        transcript = submission.get("transcript_path")
        tool_use_id = submission.get("tool_use_id")
        if _run_ended(session_id, script, transcript, float(record["minted_at"]), dirs, tool_use_id) is not None:
            continue
        if _same_path(_run_transcript_dir(session_id, script, transcript, tool_use_id, dirs), spawn_dir):
            return run_id
    return None


def _holds_ended_marker(root: Path, run_id: str) -> bool:
    """True when the run's record dir holds a `halted` or `landed` marker: such a run is never
    working, whatever an unconsumed token says. A `resumed` marker is live again."""
    marker = _read_json(_ask_record_dir(root, run_id) / _HALTED_FILE)
    return bool(marker) and marker.get("status") in (_STATUS_HALTED, _STATUS_LANDED)


def _has_launch(record: dict, dirs: list[str]) -> bool:
    """True when a launch record joins the token's session and script. A token with no submission
    record and no launch was emitted and never fired: the Workflow call writes the submission
    before it launches, so an in-flight call is never read as unfired."""
    session_id = record.get("session_id")
    script = record.get("script_path")
    if not (isinstance(session_id, str) and session_id and isinstance(script, str) and script):
        return False
    return bool(_launch_records(session_id, script, dirs))


def _unfired(root: Path, record: dict, dirs: list[str]) -> bool:
    """True for a token with no submission record and no launch, whether or not `run.json` was
    written: nothing ever fired it, so it holds no claim on its sizing. Never spends the token."""
    run_id = record["run_id"]
    if _read_json(_submission_path(root, run_id)):
        return False
    return not _has_launch(record, dirs)


def _record_live(
    root: Path,
    record: dict,
    session_id: Optional[str],
    transcript_path: Optional[str],
    dirs: list[str],
    *,
    ended_on_evidence: bool = False,
    snapshot: Optional[LaunchSnapshot] = None,
) -> bool:
    """True while the run is live; with `ended_on_evidence`, True only when the run ended on the
    harness's evidence (the one site that consumes the token)."""
    run_id = record["run_id"]
    if not ended_on_evidence and _holds_ended_marker(root, run_id):
        return False
    submission = _read_json(_submission_path(root, run_id)) or {}
    run_session = submission.get("session_id") or record.get("session_id")
    transcript = submission.get("transcript_path") or (
        transcript_path if run_session and run_session == session_id else None
    )
    script = submission.get("script_path") or record.get("script_path")
    ended = _run_ended(
        run_session,
        script,
        transcript,
        float(record["minted_at"]),
        dirs,
        submission.get("tool_use_id"),
        snapshot,
    )
    if ended == _EVIDENCE:
        _run_token.consume(root, run_id)
    if ended_on_evidence:
        return ended == _EVIDENCE
    return ended is None


def run_ended_on_evidence(
    repo_root: Path,
    record: dict,
    *,
    tmpdir: Optional[str] = None,
    snapshot: Optional[LaunchSnapshot] = None,
) -> bool:
    """True only when the harness's own evidence shows the run ended (a terminal or stopped task
    notification, a gone task, a refused submitting call); an unlaunched-grace timer end is False.
    `record` is a token record (`run_id`, `minted_at`, `script_path`, `session_id`). Spends the
    token on an evidence end, as `_record_live` does."""
    dirs = snapshot.dirs if snapshot is not None else _launch_dirs(tmpdir)
    return _record_live(
        Path(repo_root), record, None, None, dirs, ended_on_evidence=True, snapshot=snapshot
    )


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
    without a submission record (a CLI emit) when that run is this session's own. A token whose
    emitted run has no submission record and no launch is unfired and reads not live (never spent)."""
    root = Path(repo_root)
    rel = _run_token.normalize_sizing_rel(root, sizing_rel)
    if not rel:
        return None
    dirs = _launch_dirs(tmpdir)
    for record in _token_records(root):
        named = record.get("sizing_rel")
        if not isinstance(named, str) or _run_token.normalize_sizing_rel(root, named) != rel:
            continue
        if _unfired(root, record, dirs):
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
        if _unfired(root, record, dirs):
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
