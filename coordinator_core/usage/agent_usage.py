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


def _errored_ids(raw: str) -> set:
    """tool_use ids whose tool_result block on this transcript line has is_error true."""
    try:
        event = json.loads(raw)
    except ValueError:
        return set()
    msg = event.get("message") if isinstance(event, dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return set()
    return {b.get("tool_use_id") for b in content
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error")}


def _messages(path: str, failed: Optional[set] = None) -> list[dict]:
    """Assistant usage messages, one per (message.id, requestId), last row wins.

    When `failed` is given, tool_use ids of errored tool_results are added to it."""
    latest: dict[Any, dict] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for n, raw in enumerate(handle):
                if failed is not None and '"is_error"' in raw and '"tool_result"' in raw:
                    failed |= _errored_ids(raw)
                parsed = usage_line(raw, n)
                if parsed is not None:
                    key, model, usage, _speed = parsed
                    prior = latest.get(key)
                    tools = prior["tools"] if prior else {}
                    if '"tool_use"' in raw:
                        tools = {**tools, **_tool_uses(raw, n)}
                    latest[key] = {"model": model, "usage": usage, "tools": tools}
    except OSError:
        return []
    return list(latest.values())


def _tool_uses(raw: str, n: int) -> dict[Any, tuple[str, Optional[str], Optional[str]]]:
    """{tool_use id: (name, input.file_path, input.command)} of one transcript line's tool_use blocks."""
    try:
        event = json.loads(raw)
    except ValueError:
        return {}
    msg = event.get("message") if isinstance(event, dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    out: dict[Any, tuple[str, Optional[str], Optional[str]]] = {}
    if not isinstance(content, list):
        return out
    for i, block in enumerate(content):
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        inp = block.get("input")
        path = inp.get("file_path") if isinstance(inp, dict) else None
        cmd = inp.get("command") if isinstance(inp, dict) else None
        out[block.get("id") or ("line", n, i)] = (
            str(block.get("name") or ""), path if isinstance(path, str) else None,
            cmd[:64] if isinstance(cmd, str) else None)
    return out


_SEARCH_TOOLS = frozenset({"Grep", "Glob"})
_SEARCH_COMMANDS = frozenset({"grep", "rg", "find"})


def _is_search(name: str, command: Optional[str]) -> bool:
    """Grep, Glob, or a Bash command whose first word is grep/rg/find.

    Deliberately narrow: `git grep` and composed commands are not counted."""
    if name in _SEARCH_TOOLS:
        return True
    if name != "Bash" or not command:
        return False
    words = command.split(None, 1)
    return bool(words) and words[0] in _SEARCH_COMMANDS


def _norm_path(path: str) -> str:
    return path.replace("\\", "/").lower()


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
    # Lazy: stall's package init reaches run_report, which imports this module.
    from coordinator_core.workflow_watch.stall import _transcript_path
    path = _transcript_path(run_dir, agent_id)
    row: dict[str, Any] = {
        "agent_id": agent_id, "label": label, "model": None, "speed": None,
        **_zero(), "unpriced": False, "unpriced_tokens": 0,
        "tool_calls": 0, "rag_calls": 0, "search_calls": 0, "rag_failed": 0, "rag_fallback": 0,
        "peak_context": 0, "max_same_file_reads": 0,
    }
    priced = 0
    models: dict[str, int] = {}
    reads: dict[str, int] = {}
    failed: set = set()
    messages = _messages(path, failed) if path else []
    rag_failed_seen = False
    for m in messages:
        usage = m["usage"]
        row["peak_context"] = max(row["peak_context"], sum(
            _num(usage, f) for f in
            ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")))
        for tid, (name, fp, cmd) in m["tools"].items():
            row["tool_calls"] += 1
            if _is_search(name, cmd):
                row["search_calls"] += 1
                if rag_failed_seen:
                    row["rag_fallback"] += 1
            if isinstance(name, str) and name.startswith("mcp__project-rag__"):
                row["rag_calls"] += 1
                if tid in failed:
                    row["rag_failed"] += 1
                    rag_failed_seen = True
            if name == "Read" and fp:
                k = _norm_path(fp)
                reads[k] = reads.get(k, 0) + 1
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
    row["max_same_file_reads"] = max(reads.values(), default=0)
    if models:
        row["model"] = max(models, key=models.__getitem__)
    else:
        model = agent_meta(run_dir, agent_id, path).get("model")
        row["model"] = model if isinstance(model, str) and model else None
    if row["unpriced"] and not priced:
        row["usd"] = None
    return row


def role_of(label: Any) -> str:
    """Leg role of an agent label: the prefix before the first ':' (work:, verify:, review:), else the label; empty is UNKNOWN."""
    if not isinstance(label, str) or not label:
        return UNKNOWN
    head = label.split(":", 1)[0]
    return head or UNKNOWN


def scan_run(run_dir: str) -> dict[str, Any]:
    """{agents, by_model, by_role, run_total} for the `started` agents of run_dir/journal.jsonl.

    usd sums priced messages only; unpriced tokens are counted in tokens and in
    unpriced_tokens, never priced as 0.
    """
    agents = [_agent_row(run_dir, aid, label)
              for aid, label in started_agents(os.path.join(run_dir, "journal.jsonl"))]
    by_model: dict[str, dict[str, Any]] = {}
    by_role: dict[str, dict[str, Any]] = {}
    total: dict[str, Any] = {**_zero(), "unpriced_tokens": 0}
    for a in agents:
        tokens = {f: a[f] for f in TOKEN_FIELDS}
        bucket = by_model.setdefault(a["model"] or UNKNOWN, {**_zero(), "agents": 0})
        bucket["agents"] += 1
        _add(bucket, tokens, a["usd"])
        rbucket = by_role.setdefault(role_of(a["label"]), {**_zero(), "agents": 0})
        rbucket["agents"] += 1
        _add(rbucket, tokens, a["usd"])
        _add(total, tokens, a["usd"])
        total["unpriced_tokens"] += a["unpriced_tokens"]
    searching = sum(1 for a in agents if a["search_calls"] or a["rag_calls"])
    rag_agents = sum(1 for a in agents if a["rag_calls"])
    total["rag_uptake"] = {"searching_agents": searching, "rag_agents": rag_agents,
                           "fallback_agents": sum(1 for a in agents if a["rag_fallback"]),
                           "ratio": rag_agents / searching if searching else None}
    return {"agents": agents, "by_model": by_model, "by_role": by_role, "run_total": total}
