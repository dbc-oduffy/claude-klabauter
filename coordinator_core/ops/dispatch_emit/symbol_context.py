"""Symbol-context contract: caller/callee entries injected into a chunk brief.

Pure types and names; no I/O at import.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from coordinator_core.hooks.example_retrieval_repo_detect import find_example_retrieval_repo_dir
from coordinator_core.ops.dispatch_emit.executor_economy import SYMBOL_CONTEXT_MAX_ENTRIES
from coordinator_core.ops.dispatch_emit.row_excerpts import symbol_spans

SYMBOL_CONTEXT_FIELD = "symbol_context"
SYMBOL_CONTEXT_UNAVAILABLE_FIELD = "symbol_context_unavailable"
REASONS = ("no-index", "stale-index", "store-error", "no-symbols")


@dataclass(frozen=True)
class SymbolContextEntry:
    """One in-repo caller or callee; `of` names the edited symbol it relates to."""

    symbol: str
    file: str
    line: int
    role: Literal["caller", "callee"]
    of: str


@dataclass(frozen=True)
class SymbolContext:
    """A chunk's entries; `reason` (one of REASONS) is set when the list is empty for a cause."""

    entries: tuple[SymbolContextEntry, ...]
    reason: str | None


_STORE = "structural_project.sqlite3"

# Role rank orders callers before callees so the cap drops callees first. The store's schema is
# example-retrieval-repo's; any drift surfaces as sqlite3.Error and degrades to "store-error".
_QUERY = """
SELECT DISTINCT 0 AS rank, 'caller' AS role, p.name, p.file, p.line, e.name AS of
FROM symbols e JOIN calls c ON c.callee_id = e.id JOIN symbols p ON p.id = c.caller_id
WHERE e.name IN ({nm}) AND e.file IN ({fl}) AND e.authority = 'project' AND p.authority = 'project'
UNION
SELECT DISTINCT 1 AS rank, 'callee' AS role, p.name, p.file, p.line, e.name AS of
FROM symbols e JOIN calls c ON c.caller_id = e.id JOIN symbols p ON p.id = c.callee_id
WHERE e.name IN ({nm}) AND e.file IN ({fl}) AND e.authority = 'project' AND p.authority = 'project'
ORDER BY rank, 4, 5, 3
LIMIT ?
"""


def _row_id(row) -> str:
    return str(row.get("id") if isinstance(row, dict) else getattr(row, "id"))


def _row_field(row, name: str):
    return row.get(name) if isinstance(row, dict) else getattr(row, name, None)


def _spans(path: str, root: Path, cache: dict):
    """Top-level spans for `path`, filled into the shared excerpt cache (same key and shape)."""
    if path not in cache:
        try:
            cache[path] = symbol_spans((root / path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            cache[path] = None
    return cache[path]


def _store_mtime(store: Path) -> float:
    # WAL mode: recent writes land in the -wal sidecar, not the main file.
    times = []
    for suffix in ("", "-wal"):
        try:
            times.append(os.stat(f"{store}{suffix}").st_mtime)
        except OSError:
            pass
    return max(times, default=0.0)


def _edited(row, root: Path, cache: dict) -> tuple[list[str], list[str]]:
    """(.py written paths, top-level symbol names the row body names) for one row."""
    writes = _row_field(row, "writes")
    body = _row_field(row, "body") or ""
    if not isinstance(writes, (list, tuple)):
        return [], []
    files = [w for w in writes if isinstance(w, str) and w.endswith(".py")]
    names: list[str] = []
    for f in files:
        for name, _a, _b in _spans(f, root, cache) or ():
            if name not in names and re.search(r"\b" + re.escape(name) + r"\b", body):
                names.append(name)
    return files, names


def _stale(files: list[str], root: Path, store_mtime: float) -> bool:
    for f in files:
        try:
            if os.stat(root / f).st_mtime > store_mtime:
                return True
        except OSError:
            continue  # the row creates this file; nothing indexed to be stale against
    return False


def lookup(repo_root, rows, cache: dict | None = None) -> dict[str, SymbolContext]:
    """In-repo callers and callees of each row's edited symbols, one read-only query per row.

    Never spawns a process. Every degrade yields empty entries plus a reason from REASONS; `cache`
    is the compose's shared excerpt span cache."""
    rows = list(rows)
    cache = {} if cache is None else cache
    out: dict[str, SymbolContext] = {}
    if repo_root is None:
        return {_row_id(r): SymbolContext((), "no-index") for r in rows}
    root = Path(repo_root)
    rag_dir = find_example_retrieval_repo_dir(str(root))
    store = Path(rag_dir) / _STORE if rag_dir else None
    if store is None or not store.is_file():
        return {_row_id(r): SymbolContext((), "no-index") for r in rows}
    store_mtime = _store_mtime(store)
    conn = None
    try:
        for row in rows:
            rid = _row_id(row)
            files, names = _edited(row, root, cache)
            if not names:
                out[rid] = SymbolContext((), "no-symbols")
                continue
            if _stale(files, root, store_mtime):
                out[rid] = SymbolContext((), "stale-index")
                continue
            try:
                if conn is None:
                    conn = sqlite3.connect(f"{store.resolve().as_uri()}?mode=ro", uri=True)
                found = conn.execute(
                    _QUERY.format(nm=",".join("?" * len(names)), fl=",".join("?" * len(files))),
                    (*names, *files, *names, *files, SYMBOL_CONTEXT_MAX_ENTRIES),
                ).fetchall()
            except sqlite3.Error:
                out[rid] = SymbolContext((), "store-error")
                continue
            out[rid] = SymbolContext(
                tuple(SymbolContextEntry(sym, file, line or 0, role, of) for _r, role, sym, file, line, of in found),
                None,
            )
    finally:
        if conn is not None:
            conn.close()
    return out
