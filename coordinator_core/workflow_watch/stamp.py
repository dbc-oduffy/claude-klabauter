
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

from coordinator_core import locked_write
from coordinator_core.workflow_watch.tail import TailReader
from coordinator_core.workflow_watch.terminal import TerminalRecord, _find_terminal_record

TERMINAL_EVENT_TYPES = frozenset({"completed", "failed", "halted", "stopped", "unknown"})

_STAMP_SOURCE = "workflow_watch"

_TYPE_BY_STATUS = {
    "completed": "completed",
    "failed": "failed",
    "killed": "stopped",
    "stopped": "stopped",
}

_LAUNCH_SCAN_LINE_CAP = 200_000


def _unescape_once(text: str) -> str | None:
    try:
        return json.loads('"' + text + '"')
    except ValueError:
        return None


def _parse_result_payload(result_text: str) -> dict | None:
    for candidate in (result_text, _unescape_once(result_text)):
        if candidate is None:
            continue
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def classify(record: TerminalRecord) -> str:
    """Map a `TerminalRecord` to one of `TERMINAL_EVENT_TYPES`.

    `killed`/`stopped` collapse to `"stopped"`; `failed` stays `"failed"`;
    `completed` is `"halted"` when `result_text` decodes (see
    `_parse_result_payload`) to a dict carrying a `"halted"` key, else
    plain `"completed"`. Never raises: a `result_text` that is absent, not
    JSON either way, or not a dict is silently treated as "no halt marker
    found" rather than a decode error.

    `record.status` outside `_TYPE_BY_STATUS`'s vocabulary (today
    unreachable — `_find_terminal_record` only constructs a `TerminalRecord`
    for a `status in _TERMINAL_STATUSES`, and `_TYPE_BY_STATUS` covers all
    four) maps to `"unknown"`, never `"completed"`: a future status added to
    `_TERMINAL_STATUSES` without a matching `_TYPE_BY_STATUS` entry must
    show up in the journal as visibly unclassified, not silently misreport
    as a clean finish.
    """
    if record.status == "completed" and record.result_text:
        payload = _parse_result_payload(record.result_text)
        if payload is not None and "halted" in payload:
            return "halted"
    return _TYPE_BY_STATUS.get(record.status, "unknown")


def _is_stamped(journal_path: str) -> bool:
    try:
        with open(journal_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("source") == _STAMP_SOURCE:
                    return True
    except OSError:
        return False
    return False


def stamp_terminal(journal_path: str, task_id: str, record: TerminalRecord) -> bool:
    """Append one terminal line to `journal_path`, unless one is already
    there. Returns whether a line was appended — `False` covers BOTH "was
    already stamped" and "an I/O error prevented it", indistinguishable to
    this function by design (see module docstring: this must never raise).

    The appended line: `{"type": <one of TERMINAL_EVENT_TYPES>, "at":
    <UTC ISO-8601>, "task_id": task_id, "status": record.status, "source":
    "workflow_watch"}` — `type` is `classify(record)`; `status` stays the
    raw harness value even when `type` is the derived `"halted"`, so a
    reader can always recover what the harness itself reported.

    Check-and-append is serialized under `locked_write.held_lock` — an
    exclusive, cross-process, cross-platform advisory lock (`fcntl` POSIX /
    `msvcrt` Windows) keyed on `journal_path` — so a live watcher and a
    concurrent `--reconcile` invocation on the SAME journal cannot both
    observe "not yet stamped" and both append (the TOCTOU this module's
    "idempotent" claim depends on). Lock acquisition failure (`LockTimeout`
    or the platform backend being unavailable) falls back to the unlocked
    check-and-append rather than raising, consistent with this module's
    fail-safe posture: a lost race here costs at most one duplicate line,
    never a crash.
    """
    target = Path(os.path.abspath(journal_path))
    lock_cm = None
    try:
        lock_cm = locked_write.held_lock(target, holder_label="workflow_watch.stamp")
        lock_cm.__enter__()
    except (RuntimeError, locked_write.LockTimeout):
        lock_cm = None
    try:
        return _stamp_terminal_locked(journal_path, task_id, record)
    except OSError:
        return False
    finally:
        if lock_cm is not None:
            lock_cm.__exit__(None, None, None)


def _stamp_terminal_locked(journal_path: str, task_id: str, record: TerminalRecord) -> bool:
    if _is_stamped(journal_path):
        return False
    line = json.dumps(
        {
            "type": classify(record),
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "task_id": task_id,
            "status": record.status,
            "source": _STAMP_SOURCE,
        }
    )
    with open(journal_path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
    return True


def _extract_task_id(node, run_id: str, _depth: int = 0) -> str | None:
    if _depth > 6:
        return None
    if isinstance(node, dict):
        if node.get("runId") == run_id and isinstance(node.get("taskId"), str):
            return node["taskId"]
        for value in node.values():
            found = _extract_task_id(value, run_id, _depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(node, list):
        for item in node:
            found = _extract_task_id(item, run_id, _depth + 1)
            if found is not None:
                return found
        return None
    return None


def _find_task_id_for_run(transcript_path: str, run_id: str) -> str | None:
    """Scan `transcript_path` line by line for the launch payload naming
    `run_id`, returning its `taskId` or `None`.

    A plain line iterator, not `TailReader`: the launch line is near the
    START of a transcript (`TailReader.poll()`'s bounded tail window is
    aimed at the opposite end, where terminal records live — see
    `reconcile()`). A cheap substring check on the raw line skips a
    `json.loads` on every line that cannot possibly match; the scan stops
    at the first positive match and is capped at `_LAUNCH_SCAN_LINE_CAP`
    lines so a pathological transcript cannot make this unbounded. Never
    raises: any `OSError` or decode failure yields `None`, the same as an
    honest "not found".
    """
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle):
                if line_no >= _LAUNCH_SCAN_LINE_CAP:
                    return None
                if run_id not in line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                task_id = _extract_task_id(payload, run_id)
                if task_id is not None:
                    return task_id
    except OSError:
        return None
    return None


def _derive_transcript_path(run_dir: str) -> str:
    session_dir = os.path.dirname(os.path.dirname(os.path.dirname(run_dir)))
    project_dir = os.path.dirname(session_dir)
    session_id = os.path.basename(session_dir)
    return os.path.join(project_dir, session_id + ".jsonl")


def reconcile(run_dir_or_journal: str) -> int:
    path = os.path.abspath(run_dir_or_journal)
    if os.path.basename(path) == "journal.jsonl":
        journal_path = path
        run_dir = os.path.dirname(path)
    else:
        run_dir = path
        journal_path = os.path.join(run_dir, "journal.jsonl")

    if _is_stamped(journal_path):
        print(f"reconcile: already stamped: {journal_path}")
        return 0

    run_id = os.path.basename(run_dir)
    transcript_path = _derive_transcript_path(run_dir)
    if not os.path.isfile(transcript_path):
        print(
            f"reconcile: no launching transcript at {transcript_path} for run {run_id}",
            file=sys.stderr,
        )
        return 1

    task_id = _find_task_id_for_run(transcript_path, run_id)
    if task_id is None:
        print(
            f"reconcile: no launch record for run {run_id} in {transcript_path}",
            file=sys.stderr,
        )
        return 1

    reader = TailReader(transcript_path, seek_to_tail=True)
    text = reader.poll()
    record = _find_terminal_record(text, task_id) if text else None
    if record is None:
        print(
            f"reconcile: no terminal record for task {task_id} in {transcript_path}",
            file=sys.stderr,
        )
        return 1

    if stamp_terminal(journal_path, task_id, record):
        print(f"reconcile: stamped task {task_id}: {classify(record)}")
        return 0

    print(f"reconcile: could not write {journal_path}", file=sys.stderr)
    return 1
