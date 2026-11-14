"""Row excerpts for executor briefs: the row body plus top-level symbol line spans per written .py file.

Pure file reads; never called from a hook. A regex line scan locates symbols (no ast).
"""
from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.ops.dispatch_emit.executor_economy import (
    EXCERPT_HEADING,
    EXCERPT_MAX_BYTES,
    EXCERPT_MAX_SYMBOLS_PER_FILE,
)

_TOP_LEVEL = re.compile(r"^(?:async def|def|class)\s+([A-Za-z_]\w*)")


def symbol_spans(text: str) -> list[tuple[str, int, int]]:
    """Top-level def/async def/class as (name, start, end), 1-based inclusive; end = next start - 1 or EOF."""
    lines = text.splitlines()
    starts: list[tuple[str, int]] = []
    for i, line in enumerate(lines, 1):
        m = _TOP_LEVEL.match(line)
        if m:
            starts.append((m.group(1), i))
    spans = []
    for idx, (name, start) in enumerate(starts):
        end = starts[idx + 1][1] - 1 if idx + 1 < len(starts) else len(lines)
        spans.append((name, start, end))
    return spans


_SENTENCE_END = re.compile(r"[.!?][\"')\]`*]*(?=\s)")


def _bytes(s: str) -> int:
    return len(s.encode("utf-8"))


def full_text_pointer(plan_path: str | None, row_id: str | None = None, section: str | None = None) -> str:
    """Where the uncut text lives: the plan file, plus the row id or section heading when known."""
    where = f"`{plan_path}`" if plan_path else "the plan"
    if row_id:
        return f"{where} row `{row_id}`"
    if section:
        return f"{where} `## {section}`"
    return where


def cut_whole(text: str, limit: int, pointer: str, *, size=len) -> str:
    """``text`` unchanged when ``size(text) <= limit``; else the longest head ending at a whole sentence
    or line that fits, plus a marker naming ``pointer`` (the full text's location). ``size`` measures
    ``limit`` (``len`` for characters, ``_bytes`` for bytes). A head with no sentence or line boundary
    falls back to the last word boundary."""
    if size(text) <= limit:
        return text
    marker = f"\n(cut at a whole sentence; full text: {pointer})"
    room = limit - size(marker)
    lo, hi = 0, (len(text) if room > 0 else 0)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if size(text[:mid]) <= room:
            lo = mid
        else:
            hi = mid - 1
    head = text[:lo]
    at_edge = lo < len(text) and text[lo].isspace()
    ends = [m.end() for m in _SENTENCE_END.finditer(head)]
    if at_edge and head and head[-1] in ".!?":
        ends.append(len(head))
    cut = max(ends + [head.rfind("\n")])
    if cut <= 0:
        cut = max(head.rfind(" "), 0)
        marker = f"\n(cut at a word boundary; full text: {pointer})"
    return text[:cut].rstrip() + marker


def _cap(text: str, limit: int = EXCERPT_MAX_BYTES, pointer: str = "the plan") -> str:
    return cut_whole(text, limit, pointer, size=_bytes)


def _file_line(path: str, repo_root: Path, row_body: str, cache: dict) -> str:
    if not path.endswith(".py"):
        return path
    if path not in cache:
        target = Path(repo_root) / path
        try:
            cache[path] = symbol_spans(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            cache[path] = None
    spans = cache[path]
    if not spans:
        return path
    named = [s for s in spans if re.search(r"\b" + re.escape(s[0]) + r"\b", row_body)]
    rest = [s for s in spans if s not in named]
    picked = (named + rest)[:EXCERPT_MAX_SYMBOLS_PER_FILE]
    return f"{path}: " + ", ".join(f"{n} L{a}-{b}" for n, a, b in picked)


def excerpt_block(
    row_body: str, paths, repo_root, head_sha: str, cache: dict,
    plan_path: str | None = None, row_id: str | None = None,
) -> str:
    """EXCERPT_HEADING, the row body, per-path symbol spans and a staleness note, capped at EXCERPT_MAX_BYTES.

    An over-cap cut lands on a whole sentence or line and names ``plan_path``'s ``row_id``."""
    pointer = full_text_pointer(plan_path, row_id)
    # The body takes at most half the budget so a long row body cannot crowd out the symbol spans.
    parts = [EXCERPT_HEADING, _cap(row_body.rstrip("\n"), EXCERPT_MAX_BYTES // 2, pointer)]
    for p in paths:
        parts.append(_file_line(p, Path(repo_root), row_body, cache))
    parts.append(
        f"Spans are at compose HEAD {head_sha}; a predecessor row writing the file shifts them (re-locate by name)."
    )
    return _cap("\n".join(parts), EXCERPT_MAX_BYTES, pointer)
