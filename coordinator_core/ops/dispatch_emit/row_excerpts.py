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
_TRUNC = "(truncated)"


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


def _cap(text: str, limit: int = EXCERPT_MAX_BYTES) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    room = limit - len(_TRUNC.encode("utf-8")) - 1
    head = raw[: max(room, 0)].decode("utf-8", errors="ignore")
    return head + "\n" + _TRUNC


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


def excerpt_block(row_body: str, paths, repo_root, head_sha: str, cache: dict) -> str:
    """EXCERPT_HEADING, the row body, per-path symbol spans and a staleness note, capped at EXCERPT_MAX_BYTES."""
    # The body takes at most half the budget so a long row body cannot crowd out the symbol spans.
    parts = [EXCERPT_HEADING, _cap(row_body.rstrip("\n"), EXCERPT_MAX_BYTES // 2)]
    for p in paths:
        parts.append(_file_line(p, Path(repo_root), row_body, cache))
    parts.append(
        f"Spans are at compose HEAD {head_sha}; a predecessor row writing the file shifts them (re-locate by name)."
    )
    return _cap("\n".join(parts))
