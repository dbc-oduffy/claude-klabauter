"""
coordinator_core.workflow_watch.terminal — "has the run with task id T ended?"

Purpose: answer that single question against the launching session
transcript, via `tail.py`'s incremental reader and nothing else — no
subprocess, no journal read, no path derivation of its own.

Two matchers, both keyed on the harness TASK id (never the `wf_` run id —
see the plan's Anti-scope: the terminal records this module looks for are
keyed on task id, and the run id appears only in the launch payload):

    1. a `<task-notification>` block containing `<task-id>T</task-id>`,
       whose `<status>` is one of `completed`, `failed`, `killed`,
       `stopped` — ALL four are terminal, not just `completed` (a detector
       matching only the happy path is silent through exactly the runs an
       EM most needs to hear about).
    2. a TaskStop result: the literal text `Successfully stopped task: T`.

Fail-safe by construction, not by convention: an unrecognised transcript
shape, an unreadable file, or a transient stat error must leave the caller
polling until its own wall-clock cap (see C1b) — this module never raises
and never reports terminal on anything it cannot positively match. Anthropic
documents the session transcript format as "internal to Claude Code and
changes between versions" (see the plan's Platform assumptions); this module
is the blast wall for that — a shape it does not recognise is silence, never
a guess.

Negative-spec: this module does NOT read the `journal.jsonl` run journal —
that is `render.py`'s (C2) job, over a different file, for a different
question ("what happened during the run" vs "has the run ended"). It does
NOT interpret balance (`started == result + failed`) as a termination
signal — the plan's Anti-scope rules that out as a false-close vector, not
merely as redundant.
"""

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

#: Characters a sentence-shaped log line can leave stuck to the end of an id.
_ID_TRAILING_PUNCT = ".,;:!?)]}\"'"


@dataclass(frozen=True)
class TerminalRecord:
    """One positively-matched terminal record for a single task id.

    `status` is always one of the harness's own four values
    (`completed`/`failed`/`killed`/`stopped` — the TaskStop path fills in
    the literal `"stopped"`, matching `check()`'s existing collapse).
    `result_text` is the raw `<result>...</result>` payload found beside a
    `<task-notification>`'s `<status>`, when present — a TaskStop match
    carries no such payload and leaves it `None`. Consumers that need to
    tell a script's own `{ halted: ... }` return apart from an ordinary
    `{ completed: ... }` one (`stamp.py`) read `result_text`; `check()`
    itself never looks inside it.
    """

    status: str
    result_text: str | None = None


def _find_terminal_record(text: str, task_id: str) -> TerminalRecord | None:
    """Match `text` against both terminal shapes for `task_id`, returning
    the first positive match found — same fail-safe posture as `check()`
    (see its own docstring): an unrecognised shape or a non-matching task
    id is `None`, never a guess.
    """
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
    """Polls a launching session transcript for a terminal record matching
    one task id, via a shared `TailReader`.

    Holds no state beyond the `TailReader` it owns — a caller polls by
    calling `check_record()` on its own cadence; this class does not sleep,
    spawn, or loop on its own (that is C1b's poll loop, over this class).
    """

    def __init__(self, transcript_path: str, task_id: str):
        self._task_id = task_id
        self._reader = TailReader(transcript_path)

    def check_record(self) -> TerminalRecord | None:
        """Poll once; return the full `TerminalRecord` (status plus, for a
        `<task-notification>` match, the sibling `<result>` payload) or
        `None` on no match yet — see `check()` for the fail-safe contract
        this shares. `stamp.py` is the one caller that needs `result_text`,
        to tell an ordinary `{ completed: ... }` script return apart from a
        `{ halted: ... }` one; `check()` itself still never looks inside it.
        """
        text = self._reader.poll()
        if not text:
            return None
        return _find_terminal_record(text, self._task_id)
