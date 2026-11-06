"""Docs-checker citation parsing and run-scoped transcript backing. Pure, no spawn.

Backing only: a `[live:]` citation is checked like `[rag:]` and never classified.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple

_CITE = re.compile(r"\[(rag|live):\s*(\S+)\s+([^\]]+?)\s*\]")


class Citation(NamedTuple):
    """One `[rag: <tool> <subject>]` or `[live: <tool> <subject>]` citation."""

    form: str
    tool: str
    subject: str


def parse(text: str) -> list[Citation]:
    """Every rag/live citation in `text`, in order."""
    return [Citation(m.group(1), m.group(2), m.group(3)) for m in _CITE.finditer(text)]


def tool_calls(transcript: Path) -> list[tuple[str, str]]:
    """(tool name, json.dumps(input)) per tool_use block; unreadable lines are skipped."""
    calls: list[tuple[str, str]] = []
    try:
        fh = open(transcript, encoding="utf-8", errors="replace")
    except OSError:
        return calls
    with fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            msg = obj.get("message") if isinstance(obj, dict) else None
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    calls.append(
                        (
                            str(block.get("name", "")),
                            json.dumps(block.get("input"), ensure_ascii=False),
                        )
                    )
    return calls


def unbacked(cites: list[Citation], calls: list[tuple[str, str]]) -> list[Citation]:
    """Citations with no call whose final `__` name segment equals the tool and whose input holds the subject."""
    out = []
    for c in cites:
        subject = json.dumps(c.subject, ensure_ascii=False)[1:-1]
        if not any(
            name.rsplit("__", 1)[-1] == c.tool and (c.subject in text or subject in text)
            for name, text in calls
        ):
            out.append(c)
    return out
