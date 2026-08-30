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

from coordinator_core.workflow_watch.tail import TailReader

RESULT_TRUNCATE_BYTES = 2048

_TRUNCATE_MARKER = "…[truncated]"


def _load_meta(run_dir: str, agent_id: str, cache: dict) -> dict:
    """Read `agent-<agent_id>.meta.json` from the run directory.

    Never raises: an absent file, a permission error, or malformed JSON all
    return `{}` — callers fall back to a placeholder label rather than
    losing the event entirely. A non-dict JSON value is likewise treated as
    absent.
    """
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

    # Cache HITS only, never misses. An agent's meta file is written once at
    # spawn and never mutated, so a successful read is good for the life of the
    # run. A miss is not: a `started` event can be rendered before the meta
    # file lands, and caching that empty result would label the agent
    # "unknown-agent" for every later event it appears in.
    # (Review: overengineering-reviewer #6 -- per-event re-read in a poll loop.)
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


def _render_event(event: dict, run_dir: str, cache: dict) -> str | None:
    """Render one parsed journal event to a single short line, or `None`
    if the event does not carry a recognised `type`/`agentId` pair (an
    unrecognised event is silence, never a guess — matching terminal.py's
    fail-safe posture over an undocumented, harness-owned file shape).
    """
    event_type = event.get("type")
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
    """Incrementally renders `journal.jsonl` events into short lines, via a
    `TailReader` it owns.

    Holds no notion of "the run has ended" — that is `TerminalWatcher`'s
    job; this class only turns journal bytes
    into lines, deduplicated by a seen-set so a caller can `poll()` on its
    own cadence without ever re-printing an event it already rendered.
    """

    def __init__(self, journal_path: str):
        self._run_dir = os.path.dirname(journal_path)
        self._reader = TailReader(journal_path)
        self._seen: set[tuple[str, str]] = set()
        # Per-instance, not module-level. An agent meta file is immutable once
        # written, so caching hits is safe for the life of a run -- but scoping
        # that to the renderer keeps the lifetime tied to the run rather than to
        # the host process, so importing this module as a library cannot
        # accumulate every run's metadata. (Review: code-reviewer slice 1, P2.)
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
            if not isinstance(event_type, str) or not isinstance(agent_id, str):
                continue

            identity = (agent_id, event_type)
            if identity in self._seen:
                continue

            line = _render_event(event, self._run_dir, self._meta_cache)
            if line is None:
                continue

            self._seen.add(identity)
            rendered.append(line)

        return rendered
