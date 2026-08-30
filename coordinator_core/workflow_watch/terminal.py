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

from coordinator_core.workflow_watch.tail import TailReader

_TASK_NOTIFICATION_RE = re.compile(
    r"<task-notification>.*?<task-id>(?P<task_id>[^<]*)</task-id>.*?"
    r"<status>(?P<status>[^<]*)</status>.*?</task-notification>",
    re.DOTALL,
)

_TASK_STOP_RE = re.compile(r"Successfully stopped task:\s*(?P<task_id>\S+)")

_TERMINAL_STATUSES = frozenset({"completed", "failed", "killed", "stopped"})

#: Characters a sentence-shaped log line can leave stuck to the end of an id.
_ID_TRAILING_PUNCT = ".,;:!?)]}\"'"


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
    calling `check()` on its own cadence; this class does not sleep, spawn,
    or loop on its own (that is C1b's poll loop, over this class).
    """

    def __init__(self, transcript_path: str, task_id: str):
        self._task_id = task_id
        self._reader = TailReader(transcript_path)

    def check(self) -> str | None:
        """Poll once; return the observed terminal status string, or
        `None` if no terminal record for this watcher's task id has been
        seen yet.

        Returned values, on a match:
            - one of `"completed"`, `"failed"`, `"killed"`, `"stopped"`
              (from a `<task-notification>` match)
            - the literal `"stopped"` (from a TaskStop match — a
              TaskStop carries no distinct status of its own, and
              "stopped" is already one of the four notification statuses,
              so callers see one closed vocabulary regardless of which
              matcher fired)

        Never raises. A read failure inside `TailReader.poll()` is already
        absorbed there (returns the unchanged buffer); this method further
        treats any transcript shape it does not recognise as "not yet" —
        it never guesses and never reports terminal without a positive
        match against this watcher's own task id.
        """
        text = self._reader.poll()
        if not text:
            return None

        for match in _TASK_NOTIFICATION_RE.finditer(text):
            if match.group("task_id") != self._task_id:
                continue
            status = match.group("status")
            if status in _TERMINAL_STATUSES:
                return status

        for match in _TASK_STOP_RE.finditer(text):
            if _clean_task_id(match.group("task_id")) == self._task_id:
                return "stopped"

        return None
