
from __future__ import annotations

import re
from dataclasses import dataclass

from coordinator_core.workflow_watch.tail import TailReader

_TASK_NOTIFICATION_RE = re.compile(
    r"<task-notification>.*?<task-id>(?P<task_id>[^<]*)</task-id>.*?"
    r"<status>(?P<status>[^<]*)</status>"
    r"(?:.*?<result>(?P<result>.*?)</result>)?.*?</task-notification>",
    re.DOTALL,
)

_TASK_STOP_RE = re.compile(r"Successfully stopped task:\s*(?P<task_id>\S+)")

_TERMINAL_STATUSES = frozenset({"completed", "failed", "killed", "stopped"})

_ID_TRAILING_PUNCT = ".,;:!?)]}\"'"


@dataclass(frozen=True)
class TerminalRecord:

    status: str
    result_text: str | None = None


def _find_terminal_record(text: str, task_id: str) -> TerminalRecord | None:
    for match in _TASK_NOTIFICATION_RE.finditer(text):
        if match.group("task_id") != task_id:
            continue
        status = match.group("status")
        if status in _TERMINAL_STATUSES:
            return TerminalRecord(status=status, result_text=match.group("result"))

    for match in _TASK_STOP_RE.finditer(text):
        if _clean_task_id(match.group("task_id")) == task_id:
            return TerminalRecord(status="stopped", result_text=None)

    return None


def _clean_task_id(raw: str) -> str:
    r"""Strip sentence punctuation the `\S+` capture swallows.

    `Successfully stopped task: <id>` is prose, so the id can be followed by a
    period or a closing bracket with no whitespace between. `\S+` takes those
    into the capture and the equality check then fails. That direction is
    fail-safe -- it never false-closes -- but it fails SILENTLY: a run that
    really did stop goes unnoticed until the wall-clock cap, which is the
    outcome this watcher exists to avoid. (Review: code-reviewer slice 1, P2.)
    """
    return raw.rstrip(_ID_TRAILING_PUNCT)


class TerminalWatcher:

    def __init__(self, transcript_path: str, task_id: str):
        self._task_id = task_id
        self._reader = TailReader(transcript_path)

    def check_record(self) -> TerminalRecord | None:
        text = self._reader.poll()
        if not text:
            return None
        return _find_terminal_record(text, self._task_id)
