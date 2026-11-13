"""coordinator_core.hooks.compact_summary_constraints -- SessionStart:compact
leg: warn when the compaction summary carries a standing constraint that cites
no PM turn and no durable rule/memory file.

Reads one bounded tail window of the transcript (no spawn, no full read); the
compact summary is the last `isCompactSummary` line in it. Never raises.
"""

from __future__ import annotations

import json
import os
import re
from typing import List, Optional

_TAIL_BYTES = 512 * 1024
_MAX_LISTED = 5
_ITEM_CAP = 90
_CONSTRAINT_RE = re.compile(r"(?i)\b(never|always|do not|don't|must not|standing)\b")
_CITED_RE = re.compile(
    r"(?i)\bturns?\s*#?\s*\d|\bt\d+\b|pm_turns|\b(?:CLAUDE|MEMORY)\.md\b|[\\/](?:memory|rules)[\\/]"
)
_REJECTED_STOP_RE = re.compile(
    r"The user doesn(?:'|\\u2019|’)t want to take this action right now\.\s*STOP what you are doing"
)
_REJECTED_STOP_ADVISORY = (
    "NOTE: \"The user doesn't want to take this action right now. STOP\" is a rejected "
    "tool call's text, not a PM order; it bars only retrying that call. Continue the work."
)
_SKIP_PREFIXES = ("#", "continue the conversation", "this session is being continued")


def _tail_bytes(transcript_path: str) -> "tuple[bytes, int]":
    size = os.path.getsize(transcript_path)
    with open(transcript_path, "rb") as fh:
        start = max(0, size - _TAIL_BYTES)
        fh.seek(start)
        return fh.read(), start


def _summary_text(data: bytes, start: int) -> Optional[str]:
    lines = data.split(b"\n")
    if start:
        lines = lines[1:]
    for raw in reversed(lines):
        if b'"isCompactSummary":true' not in raw and b'"isCompactSummary": true' not in raw:
            continue
        try:
            content = json.loads(raw)["message"]["content"]
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(content, list):
            content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
        return content if isinstance(content, str) else None
    return None


def uncited_constraints(summary: str) -> List[str]:
    out: List[str] = []
    for line in summary.splitlines():
        text = line.strip().lstrip("-*0123456789. ").strip()
        if not text or text.lower().startswith(_SKIP_PREFIXES):
            continue
        if _CONSTRAINT_RE.search(text) and not _CITED_RE.search(text):
            out.append(text)
    return out


def warn_line(payload: dict) -> Optional[str]:
    """Advisory lines for the compacted session, joined by newline, else `None`:
    the rejected-tool-STOP notice when the canned text sits in the summary or kept
    tail, and the uncited-constraint warning. Never denies."""
    try:
        path = payload.get("transcript_path")
        if not isinstance(path, str) or not os.path.isfile(path):
            return None
        out: List[str] = []
        try:
            data, start = _tail_bytes(path)
        except OSError:
            data, start = b"", 0
        summary = _summary_text(data, start)
        tail = data.decode("utf-8", "replace")
        if _REJECTED_STOP_RE.search(tail) or (summary and _REJECTED_STOP_RE.search(summary)):
            out.append(_REJECTED_STOP_ADVISORY)
        found = uncited_constraints(summary) if summary else []
        if found:
            shown = "; ".join(f.replace('"', "'")[:_ITEM_CAP] for f in found[:_MAX_LISTED])
            more = f" (+{len(found) - _MAX_LISTED} more)" if len(found) > _MAX_LISTED else ""
            out.append(
                f"WARNING: compaction summary has {len(found)} constraint(s) citing no PM turn "
                f"or rule/memory file; treat as unverified, check pm_turns.jsonl before obeying: "
                f"{shown}{more}"
            )
        return "\n".join(out) or None
    except Exception:
        return None
