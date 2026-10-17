"""Per-agent token split, model tag and list-price USD over a Workflow run dir.

Reads the run's journal for `started` agents and each agent transcript in full (a
`"usage"` substring prefilter keeps json.loads off non-usage lines). Pricing is per
message through price_table.estimate_usd and summed, never priced on an aggregate.
No subprocess, no writes.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from coordinator_core.usage.price_table import TOKEN_FIELDS, estimate_usd
from coordinator_core.usage.session_cost import usage_line
from coordinator_core.workflow_watch.stall import _transcript_path

UNKNOWN = "unknown"


def started_agents(journal_path: str) -> list[tuple[str, str]]:
    """(agentId, label) of every `started` event, in journal order, one per agent id."""
    seen: dict[str, str] = {}
    try:
        with open(journal_path, encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                if '"started"' not in raw:
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(event, dict) or event.get("type") != "started":
                    continue
                agent_id = event.get("agentId")
                if not isinstance(agent_id, str) or agent_id in seen:
                    continue
                label = event.get("label")
                seen[agent_id] = label if isinstance(label, str) and label else agent_id
    except OSError:
        return []
    return list(seen.items())


def agent_meta(run_dir: str, agent_id: str, transcript: Optional[str]) -> dict:
    """The first readable `agent-<id>.meta.json` beside the transcript or under run_dir; {} when none."""
    name = f"agent-{agent_id}.meta.json"
    candidates = [os.path.join(run_dir, name), os.path.join(run_dir, "subagents", name)]
    if transcript:
        candidates.insert(0, transcript[: -len(".jsonl")] + ".meta.json")
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _messages(path: str) -> list[dict]:
    """Assistant usage messages, one per (message.id, requestId), last row wins."""
    latest: dict[Any, dict] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for n, raw in enumerate(handle):
                parsed = usage_line(raw, n)
                if parsed is not None:
                    key, model, usage, _speed = parsed
                    latest[key] = {"model": model, "usage": usage}
    except OSError:
        return []
    return list(latest.values())


def _num(usage: dict, field: str) -> int:
    v = usage.get(field)
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _zero() -> dict[str, Any]:
    return {**{f: 0 for f in TOKEN_FIELDS}, "total_tokens": 0, "usd": 0.0}


def _add(into: dict[str, Any], tokens: dict[str, int], usd: Optional[float]) -> None:
    for f in TOKEN_FIELDS:
        into[f] += tokens[f]
    into["total_tokens"] += sum(tokens.values())
    if usd is not None:
        into["usd"] += usd


def _agent_row(run_dir: str, agent_id: str, label: str) -> dict[str, Any]:
    path = _transcript_path(run_dir, agent_id)
    row: dict[str, Any] = {
        "agent_id": agent_id, "label": label, "model": None, "speed": None,
        **_zero(), "unpriced": False, "unpriced_tokens": 0,
    }
    priced = 0
    models: dict[str, int] = {}
    for m in (_messages(path) if path else []):
        usage = m["usage"]
        tokens = {f: _num(usage, f) for f in TOKEN_FIELDS}
        model = m["model"] if isinstance(m["model"], str) and m["model"] else None
        speed = usage.get("speed") if isinstance(usage.get("speed"), str) else None
        if speed == "fast" or row["speed"] is None:
            row["speed"] = speed
        if model:
            models[model] = models.get(model, 0) + sum(tokens.values())
        usd = estimate_usd(model, usage, speed) if model else None
        if usd is None and sum(tokens.values()) == 0:
            continue
        _add(row, tokens, usd)
        if usd is None:
            row["unpriced"] = True
            row["unpriced_tokens"] += sum(tokens.values())
        else:
            priced += 1
    if models:
        row["model"] = max(models, key=models.__getitem__)
    else:
        model = agent_meta(run_dir, agent_id, path).get("model")
        row["model"] = model if isinstance(model, str) and model else None
    if row["unpriced"] and not priced:
        row["usd"] = None
    return row


def scan_run(run_dir: str) -> dict[str, Any]:
    """{agents, by_model, run_total} for the `started` agents of run_dir/journal.jsonl.

    usd sums priced messages only; unpriced tokens are counted in tokens and in
    unpriced_tokens, never priced as 0.
    """
    agents = [_agent_row(run_dir, aid, label)
              for aid, label in started_agents(os.path.join(run_dir, "journal.jsonl"))]
    by_model: dict[str, dict[str, Any]] = {}
    total: dict[str, Any] = {**_zero(), "unpriced_tokens": 0}
    for a in agents:
        tokens = {f: a[f] for f in TOKEN_FIELDS}
        bucket = by_model.setdefault(a["model"] or UNKNOWN, {**_zero(), "agents": 0})
        bucket["agents"] += 1
        _add(bucket, tokens, a["usd"])
        _add(total, tokens, a["usd"])
        total["unpriced_tokens"] += a["unpriced_tokens"]
    return {"agents": agents, "by_model": by_model, "run_total": total}
