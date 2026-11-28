"""Per-agent token split, model tag and list-price USD over a Workflow run dir.

Reads the run's journal for `started` agents and each agent transcript in full (a
`"usage"` substring prefilter keeps json.loads off non-usage lines). Pricing is per
message through price_table.estimate_usd and summed, never priced on an aggregate.
No subprocess, no writes.
"""

from __future__ import annotations

import json
import os
import re
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


_REFUSED_MARKERS = ("connection refused", "connect refused", "econnrefused")
_TIMEOUT_MARKERS = ("timed out", "timeout", "stalled_stage", "call_deadline", "backpressure")


def _failure_kind(content: Any) -> str:
    """"refused" (definite down), "timeout" or "other" for an errored tool_result's content."""
    if isinstance(content, list):
        text = " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    else:
        text = str(content or "")
    low = text.lower()
    if any(m in low for m in _REFUSED_MARKERS):
        return "refused"
    if any(m in low for m in _TIMEOUT_MARKERS):
        return "timeout"
    return "other"


def _errored_ids(raw: str) -> dict:
    """{tool_use id: failure kind} of the errored tool_result blocks on this transcript line."""
    try:
        event = json.loads(raw)
    except ValueError:
        return {}
    msg = event.get("message") if isinstance(event, dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return {}
    return {b.get("tool_use_id"): _failure_kind(b.get("content")) for b in content
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error")}


_NOT_INDEXED_MARKERS = ("not indexed", "not_indexed")
_RESULT_LISTS = ("results", "hits", "matches", "symbols", "items")


def _result_text(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict)).strip()
    return str(content or "").strip()


def _is_empty_result(content: Any) -> bool:
    """True for a non-error tool_result that is blank, an empty JSON container, an empty result list, or says not indexed."""
    text = _result_text(content)
    if text.lower() in ("", "[]", "{}", "null", "none"):
        return True
    if len(text) < 200 and any(m in text.lower() for m in _NOT_INDEXED_MARKERS):
        return True
    if text[0] in "{[":
        try:
            data = json.loads(text)
        except ValueError:
            return False
        if isinstance(data, dict):
            return any(k in data and data[k] == [] for k in _RESULT_LISTS)
        return data == []
    return False


def _empty_ids(raw: str) -> set:
    """tool_use ids of the non-error tool_result blocks on this transcript line whose result is empty."""
    try:
        event = json.loads(raw)
    except ValueError:
        return set()
    msg = event.get("message") if isinstance(event, dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return set()
    return {b.get("tool_use_id") for b in content
            if isinstance(b, dict) and b.get("type") == "tool_result" and not b.get("is_error")
            and _is_empty_result(b.get("content"))}


def _messages(path: str, failed: Optional[dict] = None, empty: Optional[set] = None) -> list[dict]:
    """Assistant usage messages, one per (message.id, requestId), last row wins.

    When `failed` is given, errored tool_results are recorded in it as {tool_use id: kind};
    when `empty` is given, tool_use ids whose non-error result is empty are added to it."""
    latest: dict[Any, dict] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for n, raw in enumerate(handle):
                if failed is not None and '"is_error"' in raw and '"tool_result"' in raw:
                    failed.update(_errored_ids(raw))
                if empty is not None and '"tool_result"' in raw:
                    empty.update(_empty_ids(raw))
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


def _names_engine_band(inp: Any) -> bool:
    """True when the tool input's band/bands/corpus value (str or list of str) starts with `engine`."""
    if not isinstance(inp, dict):
        return False
    for key in ("band", "bands", "corpus"):
        v = inp.get(key)
        for item in (v if isinstance(v, list) else [v]):
            if isinstance(item, str) and item.strip().lower().startswith("engine"):
                return True
    return False


def _tool_uses(raw: str, n: int) -> dict[Any, tuple[str, Optional[str], Optional[str], bool]]:
    """{tool_use id: (name, input.file_path, input.command, names_engine_band)} of one transcript line's tool_use blocks."""
    try:
        event = json.loads(raw)
    except ValueError:
        return {}
    msg = event.get("message") if isinstance(event, dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    out: dict[Any, tuple[str, Optional[str], Optional[str], bool]] = {}
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
            cmd[:512] if isinstance(cmd, str) else None, _names_engine_band(inp))
    return out


_UE_ADDON_PREFIXES = ("mcp__project-rag__project_engine_", "mcp__project-rag__project_cpp_symbol")

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


_SEGMENT_SPLIT = re.compile(r"\|\||&&|[|;&\n(`]|\$\(")
_SHELLS = frozenset({"bash", "sh", "zsh"})
_COMMAND_WRAPPERS = frozenset({"xargs", "sudo", "time", "command", "env", "nohup", "exec"})


def _segment_runs_search(segment: str) -> bool:
    words = segment.split()
    while words and (words[0] in _COMMAND_WRAPPERS or ("=" in words[0] and not words[0].startswith("-"))):
        words.pop(0)
    if not words:
        return False
    head = words[0].replace("\\", "/").rsplit("/", 1)[-1]
    if head.endswith(".exe"):
        head = head[:-4]
    if head == "git":
        return "grep" in words[1:4] and "-m" not in words[1:4]
    if head in _SHELLS and "-c" in words[1:]:
        inner = segment.split("-c", 1)[1].strip().strip("\"'")
        return any(_segment_runs_search(s) for s in _SEGMENT_SPLIT.split(inner))
    return head in _SEARCH_COMMANDS


def _is_grep_call(name: str, command: Optional[str]) -> bool:
    """Grep, Glob, or a Bash command with any pipeline segment that runs grep, rg or find (incl. `git grep`)."""
    if name in _SEARCH_TOOLS:
        return True
    if name != "Bash" or not command:
        return False
    return any(_segment_runs_search(s) for s in _SEGMENT_SPLIT.split(command))


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
        "tool_calls": 0, "rag_calls": 0, "ue_calls": 0, "search_calls": 0, "grep_calls": 0, "rag_failed": 0, "rag_errors": 0,
        "rag_fallback": 0,
        "rag_timeouts": 0, "rag_refused": 0, "peak_context": 0, "max_same_file_reads": 0,
    }
    priced = 0
    models: dict[str, int] = {}
    reads: dict[str, int] = {}
    failed: dict = {}
    empty: set = set()
    messages = _messages(path, failed, empty) if path else []
    meta_type = agent_meta(run_dir, agent_id, path).get("agentType")
    row["agent_type"] = meta_type if isinstance(meta_type, str) and meta_type else None
    row["rag_positions"] = []
    row["rag_empty"] = 0
    rag_failed_seen = False
    for m in messages:
        usage = m["usage"]
        row["peak_context"] = max(row["peak_context"], sum(
            _num(usage, f) for f in
            ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")))
        for tid, (name, fp, cmd, engine_band) in m["tools"].items():
            position = row["tool_calls"]
            row["tool_calls"] += 1
            if _is_grep_call(name, cmd):
                row["grep_calls"] += 1
            if _is_search(name, cmd):
                row["search_calls"] += 1
                if rag_failed_seen:
                    row["rag_fallback"] += 1
            if isinstance(name, str) and name.startswith("mcp__project-rag__"):
                row["rag_calls"] += 1
                row["rag_positions"].append(position)
                if name.startswith(_UE_ADDON_PREFIXES) or engine_band:
                    row["ue_calls"] += 1
                if name.startswith(_UE_ADDON_PREFIXES) and tid in empty:
                    row["rag_empty"] += 1
                if tid in failed:
                    row["rag_failed"] += 1
                    row["rag_errors"] += 1
                    kind = failed[tid]
                    if kind == "timeout":
                        row["rag_timeouts"] += 1
                    elif kind == "refused":
                        row["rag_refused"] += 1
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
    total["grep_calls"] = sum(a["grep_calls"] for a in agents)
    total["rag_calls"] = sum(a["rag_calls"] for a in agents)
    total["rag_uptake"] = {"searching_agents": searching, "rag_agents": rag_agents,
                           "fallback_agents": sum(1 for a in agents if a["rag_fallback"]),
                           "ratio": rag_agents / searching if searching else None}
    return {"agents": agents, "by_model": by_model, "by_role": by_role, "run_total": total}
