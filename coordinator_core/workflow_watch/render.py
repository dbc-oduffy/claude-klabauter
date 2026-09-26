"""
coordinator_core.workflow_watch.render — turn `journal.jsonl` into lines an EM
can act on.

Purpose: answer ONE question — "what happened during this run, in a form
short enough to read on a busy console" — over `journal.jsonl`, via
`tail.py`'s incremental reader and nothing else. This is the fix for the
plan's headline failure: a hand-rolled `tail -f | grep` prints raw truncated
JSON, which matches nothing structural and is unreadable when it does match.

Negative-spec: this module does NOT read the launching session transcript —
that is `terminal.py`'s (C1) job, over a different file, answering a
different question ("has the run ended" vs "what happened during it"). It
does NOT interpret journal balance (`started == result + failed`) as
anything — the plan's Anti-scope rules that out, and this module never even
counts events, only renders them. It never emits a raw journal line — that
is precisely today's failure.

One journal event shape has no `agentId`: the single terminal line
`stamp.py` appends once a run ends (`type` one of `stamp.TERMINAL_EVENT_TYPES`,
`source: "workflow_watch"`). This module renders that line too — it is
still `journal.jsonl` content — but keys its identity on `"__terminal__"`
rather than an agent id, and never confuses it with the pre-existing
per-agent `"failed"` event type (gated on `source`, not `type` alone).

Each journal event becomes at most ONE rendered line, ever, regardless of
how many times `JournalRenderer.poll()` is called: `TailReader.poll()`
returns its whole bounded trailing buffer on every call (see tail.py), not
just newly appended bytes, so a naive per-poll render would re-print
recently-seen events on every subsequent poll even before the journal ever
shrinks. A seen-set keyed on `(agentId, type)` — the stable identity a
journal event carries; there is no `timestamp` field in the observed shape
— absorbs both that ordinary re-buffering and the rarer shrink-reset case
where `tail.py` resets its offset to 0 and re-scans the journal from the
start after a PreCompact/PostCompact-style rewrite.
"""

from __future__ import annotations

import json
import os

from coordinator_core.workflow_watch.stamp import TERMINAL_EVENT_TYPES
from coordinator_core.workflow_watch.tail import TailReader

RESULT_TRUNCATE_BYTES = 2048

_TRUNCATE_MARKER = "…[truncated]"


def _load_meta(run_dir: str, agent_id: str, cache: dict) -> dict:
    cached = cache.get((run_dir, agent_id))
    if cached is not None:
        return cached

    path = os.path.join(run_dir, f"agent-{agent_id}.meta.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not data:
        return {}

    cache[(run_dir, agent_id)] = data
    return data


def _truncate(text: str) -> str:
    """Hard-truncate `text` to `RESULT_TRUNCATE_BYTES` (UTF-8 encoded),
    never exceeding the cap even after appending the truncation marker.
    """
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= RESULT_TRUNCATE_BYTES:
        return text
    marker = _TRUNCATE_MARKER.encode("utf-8")
    budget = max(RESULT_TRUNCATE_BYTES - len(marker), 0)
    return encoded[:budget].decode("utf-8", errors="ignore") + _TRUNCATE_MARKER


def _is_terminal_stamp(event: dict, event_type) -> bool:
    return (
        isinstance(event_type, str)
        and event_type in TERMINAL_EVENT_TYPES
        and event.get("source") == "workflow_watch"
    )


def _render_event(event: dict, run_dir: str, cache: dict) -> str | None:
    event_type = event.get("type")

    if _is_terminal_stamp(event, event_type):
        status = event.get("status")
        status = status if isinstance(status, str) else "unknown"
        return f"terminal  {event_type} ({status})"

    agent_id = event.get("agentId")
    if not isinstance(event_type, str) or not isinstance(agent_id, str):
        return None

    meta = _load_meta(run_dir, agent_id, cache)
    agent_type = meta.get("agentType")
    agent_type = agent_type if isinstance(agent_type, str) else "unknown-agent"
    model = meta.get("model")
    model = model if isinstance(model, str) else "unknown-model"

    if event_type == "started":
        return f"started  {agent_type} ({model})"
    if event_type == "result":
        result = event.get("result")
        result_text = result if isinstance(result, str) else ""
        return f"result   {agent_type}: {_truncate(result_text)}"
    if event_type == "failed":
        return f"FAILED   {agent_type}"
    return None


class JournalRenderer:

    def __init__(self, journal_path: str):
        self._run_dir = os.path.dirname(journal_path)
        self._reader = TailReader(journal_path)
        self._seen: set[tuple[str, str]] = set()
        self._meta_cache: dict[tuple[str, str], dict] = {}

    def poll(self) -> list[str]:
        """Poll the journal once and return newly-rendered lines only.

        Reads via `TailReader.poll_lines()`, which yields only lines
        COMPLETED since the last call. The alternative, `poll()`, hands back
        its whole bounded buffer every time, so parsing what it returns meant
        re-parsing up to TAIL_BUFFER_BYTES once per second for the life of the
        run — ~1800 re-parses of the same bytes across 30 minutes, every one
        of them discarded by the seen-set below. (Review:
        overengineering-reviewer #2.)

        The seen-set stays, with a narrower job than it had: `poll_lines()`
        never re-delivers on the ordinary append path, but a shrink-reset (a
        compacted/rewritten journal) does re-emit from the start, and the set
        — keyed on `(agentId, type)`, never on byte offset — is what keeps
        that path from re-printing an event already rendered.

        Never raises: a malformed journal line is skipped (not a crash),
        and a missing/malformed `agent-<id>.meta.json` renders with
        placeholder labels rather than dropping the event (see
        `_load_meta`).
        """
        rendered: list[str] = []
        for raw_line in self._reader.poll_lines():
            raw_line = raw_line.strip()
            try:
                event = json.loads(raw_line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue

            event_type = event.get("type")
            agent_id = event.get("agentId")
            is_terminal = _is_terminal_stamp(event, event_type)
            if not isinstance(event_type, str):
                continue
            if not is_terminal and not isinstance(agent_id, str):
                continue

            identity = (agent_id if isinstance(agent_id, str) else "__terminal__", event_type)
            if identity in self._seen:
                continue

            line = _render_event(event, self._run_dir, self._meta_cache)
            if line is None:
                continue

            self._seen.add(identity)
            rendered.append(line)

        return rendered
