"""
coordinator_core.symbol_knowledge.traps -- KR3 one-line traps for flagged symbols.

Purpose: ``record_traps`` turns the revise and halt events already in the close-job record into one
trap line per (symbol, event) for each flagged symbol whose path an event touched.

Negative-spec:
  - Writes nothing and scans no file: the detached batch job appends the returned lines to the
    sidecar. The only read beyond the record is the sidecar, when ``root`` is passed, to drop lines
    the entry already holds.
  - Events come from ``record["inputs"]`` only: ``unlanded_paths`` (``{row id: [paths]}``) is a
    halt, ``review_only.rows`` (``[{id, paths}]``) is a revise. Malformed shapes read as no event.
  - A line is deterministic for (run, event, rows), so a re-run adds no duplicate; capped at
    ``MAX_TRAP_CHARS``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

MAX_TRAP_CHARS = 200
EVENT_HALT = "halt"
EVENT_REVISE = "revise"


def _norm(path: Any) -> str:
    return str(path).replace("\\", "/").strip().removeprefix("./")


def _row_paths(rows: Any) -> "list[tuple[str, list[str]]]":
    """``(row id, paths)`` pairs from a mapping ``{id: [paths]}`` or a list ``[{id, paths}]``."""
    pairs: "list[tuple[str, list[str]]]" = []
    if isinstance(rows, Mapping):
        items: Iterable[Any] = ((k, v) for k, v in rows.items())
    elif isinstance(rows, list):
        items = ((r.get("id"), r.get("paths")) for r in rows if isinstance(r, Mapping))
    else:
        return pairs
    for row_id, paths in items:
        if isinstance(paths, (list, tuple)) and paths:
            pairs.append((str(row_id or "?"), [_norm(p) for p in paths]))
    return pairs


def _events(record: Mapping[str, Any]) -> "list[tuple[str, str, list[str]]]":
    """``(event, row id, normalized paths)`` for each halt and revise the record carries."""
    inputs = record.get("inputs")
    if not isinstance(inputs, Mapping):
        return []
    out = [(EVENT_HALT, rid, paths) for rid, paths in _row_paths(inputs.get("unlanded_paths"))]
    review_only = inputs.get("review_only")
    if isinstance(review_only, Mapping):
        out += [(EVENT_REVISE, rid, paths) for rid, paths in _row_paths(review_only.get("rows"))]
    return out


def _line(run_id: str, event: str, rows: Sequence[str], path: str) -> str:
    text = f"{event} in run {run_id} (row {','.join(rows)}) touched {path}"
    return text if len(text) <= MAX_TRAP_CHARS else text[: MAX_TRAP_CHARS - 3] + "..."


def record_traps(
    record: Mapping[str, Any],
    flagged: Sequence[Any],
    root: Optional["str | Path"] = None,
) -> "dict[str, list[str]]":
    """Qualified name -> trap lines (one per event kind) for each flagged symbol whose path a halt or
    revise in *record* touched; a symbol with no event, or only lines its sidecar entry holds
    (when *root* is given), is absent."""
    events = _events(record)
    if not events:
        return {}
    run_id = str(record.get("run_id") or "?")
    existing: "dict[str, Any]" = {}
    if root is not None:
        from coordinator_core.symbol_knowledge.sidecar import read_sidecar

        existing = read_sidecar(root)
    out: "dict[str, list[str]]" = {}
    for symbol in flagged:
        path = _norm(symbol.path)
        held = set(existing.get(symbol.qualified_name, {}).get("traps", ()))
        lines: "list[str]" = []
        for kind in (EVENT_HALT, EVENT_REVISE):
            rows = [rid for ev, rid, paths in events if ev == kind and path in paths]
            if not rows:
                continue
            line = _line(run_id, kind, rows, path)
            if line not in held and line not in lines:
                lines.append(line)
        if lines:
            out[symbol.qualified_name] = lines
    return out
