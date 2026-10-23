"""Brief-trim contract: protected headings, per-section byte accounting, raw-dump detection.

Pure; imports only constants from ask_contract and executor_economy.
"""
from __future__ import annotations

import re

from coordinator_core.ops.dispatch_emit import ask_contract, executor_economy

# Equal to ask_stage.PM_INTENT_HEADING; not imported from there because ask_stage is heavy.
PM_INTENT_HEADING_TEXT = "## PM intent (verbatim)"
FILES_BLOCK_HEADING = "## Files you may write"
PROTECTED_HEADINGS = (
    PM_INTENT_HEADING_TEXT,
    ask_contract.RELAYED_ASK_HEADING,
    executor_economy.EXCERPT_HEADING,
    FILES_BLOCK_HEADING,
)
RAW_DUMP_MAX_FENCE_BYTES = 2000

# Summed UTF-8 bytes of every executor brief for tests/fixtures/brief_trim/plan.md, staged by
# ask_stage.stage before the trim landed; measured at HEAD 33fe0f0133ee.
BRIEF_BYTES_BASELINE = 35686

_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})")


def section_bytes(text: str) -> dict[str, int]:
    """UTF-8 bytes per `## ` section keyed by its heading line; text before the first heading is under ""."""
    out: dict[str, int] = {}
    key = ""
    buf: list[str] = []
    in_fence = None
    fence_len = 0
    for line in text.splitlines(keepends=True):
        m = _FENCE.match(line)
        if m:
            marker = m.group(2)[0]
            if in_fence is None:
                in_fence = marker
                fence_len = len(m.group(2))
            elif in_fence == marker and len(m.group(2)) >= fence_len:
                in_fence = None
        if in_fence is None and not m and line.startswith("## "):
            if buf:
                out[key] = out.get(key, 0) + len("".join(buf).encode("utf-8"))
            key = line.rstrip("\r\n")
            buf = [line]
        else:
            buf.append(line)
    out[key] = out.get(key, 0) + len("".join(buf).encode("utf-8"))
    return out


def raw_dump_findings(text: str) -> list[str]:
    """One finding per fenced code block whose body exceeds RAW_DUMP_MAX_FENCE_BYTES; [] when clean."""
    findings: list[str] = []
    fence = None
    body: list[str] = []
    start = 0
    for n, line in enumerate(text.splitlines(), 1):
        m = _FENCE.match(line)
        if fence is None:
            if m:
                fence, body, start = m.group(2), [], n
        elif m and m.group(2)[0] == fence[0] and len(m.group(2)) >= len(fence):
            size = len("\n".join(body).encode("utf-8"))
            if size > RAW_DUMP_MAX_FENCE_BYTES:
                findings.append(
                    f"fenced block at line {start} is {size} bytes (max {RAW_DUMP_MAX_FENCE_BYTES})"
                )
            fence = None
        else:
            body.append(line)
    return findings
