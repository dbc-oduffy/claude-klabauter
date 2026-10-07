"""Usage-limit stall scan over a Workflow run dir.

A rate-limited `agent()` call never settles: the journal holds a `started`
with no `result`, and the agent transcript ends on a 429 `rate_limit` turn.
Only file tails are read -- no subprocess, no whole-file read.
"""

from __future__ import annotations

import datetime
import json
import os
from dataclasses import dataclass
from typing import Optional

from coordinator_core.quota_limits import active_rate_limit

#: Distinct from the watcher's 0 (terminal), 1 (cap) and 2 (usage).
EXIT_HALTED_USAGE_LIMIT = 3

TRANSCRIPT_TAIL_BYTES = 64 * 1024
JOURNAL_TAIL_BYTES = 400 * 1024


@dataclass(frozen=True)
class UsageLimitHalt:
    run_id: str
    agent_id: str
    agent_label: str
    resets_at: float

    @property
    def resets_at_iso(self) -> str:
        return datetime.datetime.fromtimestamp(
            self.resets_at, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def resume_instruction(self, script_path: str = "<scriptPath>") -> str:
        return (f"TaskStop the task, then Workflow({{scriptPath: {script_path!r}, "
                f"resumeFromRunId: {self.run_id!r}}})")

    def as_record(self, script_path: str = "<scriptPath>") -> dict:
        return {
            "halted_by": "usage_limit",
            "resets_at": self.resets_at,
            "resets_at_iso": self.resets_at_iso,
            "agent_label": self.agent_label,
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "resume": self.resume_instruction(script_path),
        }

    def line(self, script_path: str = "<scriptPath>") -> str:
        return f"halted: {json.dumps(self.as_record(script_path), sort_keys=True)}"


def _tail_text(path: str, limit: int) -> str:
    try:
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            handle.seek(max(size - limit, 0))
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _transcript_path(run_dir: str, agent_id: str) -> Optional[str]:
    name = f"agent-{agent_id}.jsonl"
    for candidate in (os.path.join(run_dir, name), os.path.join(run_dir, "subagents", name)):
        if os.path.isfile(candidate):
            return candidate
    return None


def _unsettled(journal_text: str) -> list[tuple[str, str]]:
    """(agentId, label) of every `started` whose dispatch key never got a `result`.

    A resumed run re-dispatches under the same key with a new agent id, so a
    key with any `result` is settled even though the old agent id never is.
    """
    started: dict[str, tuple[str, str]] = {}
    settled: set[str] = set()
    for raw in journal_text.split("\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        agent_id = event.get("agentId")
        if not isinstance(agent_id, str):
            continue
        key = event.get("key")
        ident = key if isinstance(key, str) else agent_id
        kind = event.get("type")
        if kind == "started":
            label = event.get("label")
            started[ident] = (agent_id, label if isinstance(label, str) and label else agent_id)
        elif kind in ("result", "failed") and event.get("source") != "workflow_watch":
            settled.add(ident)
    return [pair for ident, pair in started.items() if ident not in settled]


def scan_for_usage_limit(journal_path: str, now: float) -> Optional[UsageLimitHalt]:
    """The first unsettled agent whose transcript ends in a live 429 rate_limit turn, else None."""
    run_dir = os.path.dirname(journal_path)
    run_id = os.path.basename(run_dir)
    for agent_id, label in _unsettled(_tail_text(journal_path, JOURNAL_TAIL_BYTES)):
        path = _transcript_path(run_dir, agent_id)
        if path is None:
            continue
        lines = [ln for ln in _tail_text(path, TRANSCRIPT_TAIL_BYTES).split("\n") if ln.strip()]
        if not lines:
            continue
        resets_at = active_rate_limit(lines[-1], now)
        if resets_at is not None:
            return UsageLimitHalt(run_id, agent_id, label, resets_at)
    return None
