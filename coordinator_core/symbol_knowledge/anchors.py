"""Symbol anchors: (path, qualified_name) -> the SCIP symbol string example-retrieval-repo indexed.

Reads example-retrieval-repo's structural store read-only (``rc_v1_symbols`` joined to ``rc_v1_symbol_scip`` on
``symbol_id``); never runs scip_python, never writes the store, never guesses. A name with zero or
several distinct SCIP strings stays unresolved and is reported as a gap for example-retrieval-repo. Keys are
split with ``freshness._in_module_segments`` so freshness and anchors address the same rows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

from coordinator_core.symbol_knowledge import freshness

SCIP_VIEW = "rc_v1_symbol_scip"
ANCHOR_RELATIONS = ("decides", "implements")

PathLike = Union[str, Path]
Key = Tuple[str, str]


def tag(sym: Optional[str]) -> str:
    """The ``[sym: <id>]`` suffix a renderer appends beside a named symbol; empty when unresolved."""
    return f" [sym: {sym}]" if sym else ""


def _scip_rows(db: sqlite3.Connection, paths: "List[str]") -> "Dict[Tuple[str, str, str], Set[str]]":
    """``(file, name, scope) -> {scip_symbol}`` for the function, method and class rows of *paths*."""
    kinds = ",".join("?" * len(freshness.SYMBOL_KINDS))
    rows: "Dict[Tuple[str, str, str], Set[str]]" = {}
    for chunk in freshness._chunks(paths):
        marks = ",".join("?" * len(chunk))
        for file, name, scope, scip in db.execute(
            f"SELECT s.file, s.name, s.scope, c.scip_symbol FROM {freshness.SYMBOLS_TABLE} s "
            f"JOIN {SCIP_VIEW} c ON c.symbol_id = s.symbol_id "
            f"WHERE s.file IN ({marks}) AND s.kind IN ({kinds})",
            (*chunk, *freshness.SYMBOL_KINDS),
        ):
            rows.setdefault((file, name, scope or ""), set()).add(scip)
    return rows


def _unique(rows: "Dict[Tuple[str, str, str], Set[str]]", path: str, segments: "List[str]") -> Optional[str]:
    if not segments:
        return None
    dotted = ".".join(segments)
    found = set(rows.get((path, dotted, ""), ()))
    if len(segments) > 1:
        found |= rows.get((path, segments[-1], ".".join(segments[:-1])), set())
    return next(iter(found)) if len(found) == 1 else None


def _read_rows(root: PathLike, paths: "List[str]") -> "Dict[Tuple[str, str, str], Set[str]]":
    """The SCIP rows of *paths* from the store under *root*; empty when the store is absent or unreadable."""
    store = freshness.store_path(root)
    if not paths or not store.is_file():
        return {}
    try:
        db = sqlite3.connect("file:" + store.resolve().as_posix() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        return _scip_rows(db, paths)
    except sqlite3.Error:
        return {}
    finally:
        db.close()


def resolve_many(root: PathLike, pairs: "Iterable[Key]") -> "Dict[Key, Optional[str]]":
    """``(path, qualified_name) -> scip symbol | None`` for every pair, in one batched read of the
    store under *root*. An absent or unreadable store resolves every pair to None."""
    keys = sorted(set(pairs))
    rows = _read_rows(root, sorted({p for p, _ in keys}))
    return {k: _unique(rows, k[0], freshness._in_module_segments(k[1])) for k in keys}


def symbols_in(root: PathLike, paths: "Iterable[str]", per_file: int = 3) -> "Dict[str, List[Dict[str, str]]]":
    """``path -> [{path, qualified_name, sym}]``: up to *per_file* symbols example-retrieval-repo indexed in each
    path, top-level before scoped, only those with exactly one SCIP string. Paths with none are absent."""
    wanted = sorted(set(paths))
    rows = _read_rows(root, wanted)
    by_file: "Dict[str, List[Tuple[str, str, str]]]" = {}
    for (file, name, scope), found in rows.items():
        if len(found) == 1:
            by_file.setdefault(file, []).append((scope, name, next(iter(found))))
    out: "Dict[str, List[Dict[str, str]]]" = {}
    for file in wanted:
        picked = sorted(by_file.get(file, ()), key=lambda r: (r[0] != "", r[0], r[1]))[:per_file]
        if picked:
            out[file] = [
                {"path": file, "qualified_name": f"{scope}.{name}" if scope else name, "sym": sym}
                for scope, name, sym in picked
            ]
    return out


def resolve(root: PathLike, path: str, qualified_name: str) -> Optional[str]:
    """The SCIP symbol string example-retrieval-repo indexed for *qualified_name* in *path*, or None."""
    return resolve_many(root, [(path, qualified_name)])[(path, qualified_name)]


def gaps(anchors: "Mapping[Key, Optional[str]]") -> "List[Key]":
    """The ``(path, qualified_name)`` pairs example-retrieval-repo could not answer, sorted, for the gap report."""
    return sorted(k for k, v in anchors.items() if v is None)


def attach(root: PathLike, items: "Sequence[Dict[str, Any]]") -> "List[Key]":
    """Set ``sym`` on each item carrying ``path`` and ``qualified_name`` that resolves, in place;
    return the unresolved pairs. Unresolved items are left without a ``sym`` key."""
    pairs = [(i["path"], i["qualified_name"]) for i in items if i.get("path") and i.get("qualified_name")]
    anchors = resolve_many(root, pairs)
    for item in items:
        sym = anchors.get((item.get("path"), item.get("qualified_name")))
        if sym:
            item["sym"] = sym
    return gaps(anchors)


def stale_anchors(
    edges: "Iterable[Mapping[str, Any]]", body_hashes: "Mapping[str, str]"
) -> "List[Mapping[str, Any]]":
    """Edges a decision or implementation hangs on whose symbol body has since changed.

    An edge is ``{symbol_key, relation, body_hash, ...}`` with ``body_hash`` the sha256 of the symbol
    body when the edge was recorded. Stale means relation is decides|implements and the current
    ``body_hashes[symbol_key]`` differs; a symbol absent from *body_hashes* carries no evidence."""
    return [
        e
        for e in edges
        if e.get("relation") in ANCHOR_RELATIONS
        and e.get("symbol_key") in body_hashes
        and body_hashes[e["symbol_key"]] != e.get("body_hash")
    ]
