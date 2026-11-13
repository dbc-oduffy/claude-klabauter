"""
coordinator_core.symbol_knowledge.freshness -- the body of ``contract.freshness`` (``code_intel.freshness``).

Purpose: for one commit, name the files whose on-disk sha256 no longer matches the hash example-retrieval-repo
indexed (``rc_v1_files``) and the changed public symbols whose indexed docstring is empty
(``rc_v1_symbols``), the tier-1 description queue. Reads example-retrieval-repo's structural store; derives
neither a hash index nor a docstring itself.

Negative-spec:
  - COMPUTE_ONLY: spawns no process, makes no model call, writes nothing; the store opens
    ``mode=ro`` and ``queued`` is returned, never handed to ``launch_batches``.
  - A missing store or table returns the ``not_applicable`` shape naming the path and table.
  - A flagged symbol with no unique ``rc_v1_symbols`` row is not queued: it lands in
    ``tier0_unresolved`` (tier 1 never stands in for tier 0).
  - Emits no ``symbol_key``: the path+qualname -> SCIP join is example-retrieval-repo's gap (``rc_v1_symbol_scip``).
  - One connection per call and one batched ``WHERE file IN (...)`` query per table.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from coordinator_core.symbol_knowledge import flag
from coordinator_core.symbol_knowledge.contract import BuildCost

OP = "code_intel.freshness"
STORE_REL = ".project-rag/structural_project.sqlite3"
FILES_TABLE = "rc_v1_files"
SYMBOLS_TABLE = "rc_v1_symbols"
SYMBOL_KINDS = ("function", "method", "class")
_IN_CHUNK = 400
_HEX_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_SCIP_DESCRIPTOR = re.compile(r"`[^`]*`/(.*)", re.S)
_SEGMENTS = re.compile(r"[#./()\[\]:!]+")

PathLike = Union[str, Path]


def store_path(root: PathLike) -> Path:
    """The example-retrieval-repo structural store of the repo at *root*."""
    return Path(root) / STORE_REL


def _not_applicable(reason: str, **extra: Any) -> "Dict[str, Any]":
    return {
        "status": "not_applicable",
        "reason": reason,
        "files_reextracted": 0,
        "files_changed": 0,
        "queued": [],
        "skipped_documented": 0,
        "tier0_unresolved": [],
        **extra,
    }


def _resolve_sha(root: Path, sha: str) -> Optional[str]:
    if _HEX_SHA.match(sha):
        return sha
    if sha == "HEAD":
        from coordinator_core.git.git_state import head_sha

        return head_sha(root)
    return None


def _in_module_segments(qualified_name: str) -> "List[str]":
    """The in-module name segments of a flagged symbol: its SCIP descriptor split on punctuation,
    or the dotted name itself when it is not a SCIP string."""
    parts = qualified_name.split(" ", 4)
    match = _SCIP_DESCRIPTOR.match(parts[4]) if len(parts) == 5 else None
    text = match.group(1) if match else qualified_name
    return [s for s in _SEGMENTS.split(text) if s]


def _chunks(items: "List[str]") -> "Iterable[List[str]]":
    for i in range(0, len(items), _IN_CHUNK):
        yield items[i : i + _IN_CHUNK]


def _indexed_hashes(db: sqlite3.Connection, paths: "List[str]") -> "Dict[str, Optional[str]]":
    out: "Dict[str, Optional[str]]" = {}
    for chunk in _chunks(paths):
        marks = ",".join("?" * len(chunk))
        for file, digest in db.execute(
            f"SELECT file, sha256 FROM {FILES_TABLE} WHERE file IN ({marks})", chunk
        ):
            out[file] = digest
    return out


def _stale_files(root: Path, db: sqlite3.Connection, paths: "List[str]") -> "List[str]":
    """Changed paths whose on-disk sha256 differs from the indexed one, or that example-retrieval-repo has no
    row for. A path no longer readable on disk is skipped."""
    indexed = _indexed_hashes(db, paths)
    stale: "List[str]" = []
    for path in paths:
        try:
            digest = hashlib.sha256((root / path).read_bytes()).hexdigest()
        except OSError:
            continue
        if indexed.get(path) != digest:
            stale.append(path)
    return stale


def _docstring_rows(
    db: sqlite3.Connection, paths: "List[str]"
) -> "Dict[Tuple[str, str, str], List[Optional[str]]]":
    """``(file, name, scope) -> [docstring, ...]`` for the function, method and class rows of *paths*."""
    kinds = ",".join("?" * len(SYMBOL_KINDS))
    rows: "Dict[Tuple[str, str, str], List[Optional[str]]]" = {}
    for chunk in _chunks(paths):
        marks = ",".join("?" * len(chunk))
        for file, name, scope, doc in db.execute(
            f"SELECT file, name, scope, docstring FROM {SYMBOLS_TABLE} "
            f"WHERE file IN ({marks}) AND kind IN ({kinds})",
            (*chunk, *SYMBOL_KINDS),
        ):
            rows.setdefault((file, name, scope or ""), []).append(doc)
    return rows


def _match(rows: "Dict[Tuple[str, str, str], List[Optional[str]]]", path: str, segments: "List[str]") -> "List[Optional[str]]":
    """Docstrings of the rows matching a symbol: the dotted in-module name with empty scope (how
    methods are stored) or the last segment under its dotted prefix scope."""
    if not segments:
        return []
    dotted = ".".join(segments)
    found = list(rows.get((path, dotted, ""), ()))
    if len(segments) > 1:
        found += rows.get((path, segments[-1], ".".join(segments[:-1])), ())
    return found


def freshness(root: PathLike, sha: str) -> "Dict[str, Any]":
    """Stale files and tier-1 queue for commit *sha* of the repo at *root*; see the module docstring.
    Returns ``{status, files_changed, files_reextracted, stale_files, queued, skipped_documented,
    tier0_unresolved, flag_detail, cost}``."""
    started = time.process_time()
    repo = Path(root)
    store = store_path(repo)
    if not store.is_file():
        return _not_applicable("store absent", store=STORE_REL, table=FILES_TABLE)
    full = _resolve_sha(repo, sha)
    if full is None:
        return _not_applicable(f"commit unresolvable: {sha}")
    from coordinator_core.git.git_dir import resolve_git_common_dir

    common = resolve_git_common_dir(repo)
    changed, changed_detail = flag.changed_paths(common, full)
    try:
        db = sqlite3.connect("file:" + store.resolve().as_posix() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return _not_applicable(f"store unreadable: {type(exc).__name__}", store=STORE_REL)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        for table in (FILES_TABLE, SYMBOLS_TABLE):
            if table not in tables:
                return _not_applicable("table absent", store=STORE_REL, table=table)
        stale = _stale_files(repo, db, changed)
        from coordinator_core.symbol_knowledge.leg import index_graph_db

        graph_db = index_graph_db(repo)
        if graph_db is None:
            flagged, flag_detail = [], "no symbol index for this repo"
        else:
            flagged, flag_detail = flag.flag_symbols_detail(common, full, graph_db)
        rows = _docstring_rows(db, sorted({f.path for f in flagged}))
    except sqlite3.Error as exc:
        return _not_applicable(f"store unreadable: {type(exc).__name__}", store=STORE_REL)
    finally:
        db.close()

    queued: "List[Dict[str, Any]]" = []
    unresolved: "List[str]" = []
    documented = 0
    for sym in flagged:
        found = _match(rows, sym.path, _in_module_segments(sym.qualified_name))
        if len(found) != 1:
            unresolved.append(sym.qualified_name)
        elif found[0] is None or not found[0].strip():
            queued.append(
                {"qualified_name": sym.qualified_name, "path": sym.path, "kind": sym.kind, "public": sym.public}
            )
        else:
            documented += 1
    cost = BuildCost(
        op=OP,
        process_ms=round((time.process_time() - started) * 1000, 3),
        spawns=0,
        tokens_in=0,
        tokens_out=0,
        files_reextracted=len(stale),
    )
    return {
        "status": "ok",
        "files_changed": len(changed),
        "files_reextracted": len(stale),
        "stale_files": stale,
        "queued": queued,
        "skipped_documented": documented,
        "tier0_unresolved": unresolved,
        "flag_detail": flag_detail if changed else changed_detail,
        "cost": asdict(cost),
    }
