"""Agent-graph extractor for emitted Workflow scripts, used to compare a composed pipeline script against the frozen oracle.

`agent_graph(script_text)` returns one entry per real `agent(` call site in source order:
`{agentType, model, has_schema, fanned}`, where `fanned` means the call sits inside an
`inChunks(` callback. `subjects(script_text)` returns the subject keys the script carries.
Masking (comments, string interiors) is the hook's own, so a call named inside a prompt or a
comment is never counted.
"""

from __future__ import annotations

import json
import re

from coordinator_core.hooks.block_workflow_unmodeled_agent import (
    _string_mask,
    _strip_comments,
)

_AGENT_TYPE_RE = re.compile(r"\bagentType\s*:\s*['\"]([^'\"]+)['\"]")
_MODEL_RE = re.compile(r"\bmodel\s*:\s*['\"]([^'\"]+)['\"]")
_SCHEMA_RE = re.compile(r"\bschema\s*:")
_IDENT = re.compile(r"[A-Za-z0-9_$.]")
_OPEN, _CLOSE = "([{", ")]}"


def _matching_close(buf: str, mask: bytearray, open_idx: int) -> int:
    """Index of the bracket closing `buf[open_idx]`, skipping masked characters; -1 if unbalanced."""
    depth = 0
    for k in range(open_idx, len(buf)):
        if mask[k]:
            continue
        if buf[k] in _OPEN:
            depth += 1
        elif buf[k] in _CLOSE:
            depth -= 1
            if depth == 0:
                return k
    return -1


def _call_sites(buf: str, mask: bytearray, name: str) -> list[tuple[int, int]]:
    """(open_paren, close_paren) of every real call `name(`, never a declaration or member access."""
    sites = []
    token = f"{name}("
    i = 0
    while True:
        pos = buf.find(token, i)
        if pos == -1:
            return sites
        i = pos + 1
        if mask[pos] or (pos > 0 and _IDENT.match(buf[pos - 1])):
            continue
        if buf[:pos].rstrip().endswith("function"):
            continue
        open_idx = pos + len(name)
        close_idx = _matching_close(buf, mask, open_idx)
        if close_idx != -1:
            sites.append((open_idx, close_idx))


def _options_object(buf: str, mask: bytearray, open_idx: int, close_idx: int) -> str:
    """Depth-1 projection of the call's last top-level `{...}` argument (nested content blanked)."""
    last = None
    depth = 0
    for k in range(open_idx, close_idx + 1):
        if mask[k]:
            continue
        ch = buf[k]
        if ch == "{" and depth == 1:
            last = k
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth -= 1
    if last is None:
        return ""
    end = _matching_close(buf, mask, last)
    out = []
    nest = 0
    for k in range(last, end + 1):
        ch = buf[k]
        if not mask[k] and ch in _OPEN:
            nest += 1
            out.append(ch if nest == 1 else " ")
        elif not mask[k] and ch in _CLOSE:
            out.append(ch if nest == 1 else " ")
            nest -= 1
        else:
            out.append(ch if nest == 1 else " ")
    return "".join(out)


def agent_graph(script_text: str) -> list[dict]:
    buf = _strip_comments(script_text)
    mask = _string_mask(buf)
    chunk_spans = _call_sites(buf, mask, "inChunks")
    graph = []
    for open_idx, close_idx in _call_sites(buf, mask, "agent"):
        options = _options_object(buf, mask, open_idx, close_idx)
        agent_type = _AGENT_TYPE_RE.search(options)
        model = _MODEL_RE.search(options)
        graph.append(
            {
                "agentType": agent_type.group(1) if agent_type else None,
                "model": model.group(1) if model else None,
                "has_schema": bool(_SCHEMA_RE.search(options)),
                "fanned": any(lo < open_idx and close_idx < hi for lo, hi in chunk_spans),
            }
        )
    return graph


def subjects(script_text: str) -> list[str]:
    """Subject keys from the script's top-level `const subjects = [...]` literal, in order."""
    match = re.search(r"^const subjects\s*=\s*", script_text, re.MULTILINE)
    if match is None:
        return []
    value, _ = json.JSONDecoder().raw_decode(script_text, match.end())
    return [item["subject"] if isinstance(item, dict) else item for item in value]
