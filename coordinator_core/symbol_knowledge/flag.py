"""
coordinator_core.symbol_knowledge.flag -- flag the public symbols a commit changed.

Purpose: the body of ``contract.flag_symbols``. Diffs the commit's tree against its first parent's
(tree objects read in-process, descending only into differing subtrees), line-diffs each changed
``.py`` blob pair with difflib, converts the parent-side hunks to byte ranges, and intersects them
with the spans in rag's ``scip_python.symtab.sqlite3`` (opened ``mode=ro``) that sits beside the
repo's ``graph.db``. The flagged key is the symtab's full SCIP symbol string, rag's qualified name.

Negative-spec:
  - Spawns no git process and writes nothing; the symtab is only read.
  - The symtab lags HEAD, so hunks are mapped on the parent side. A symbol the commit adds is not in
    the symtab yet and is flagged on a later run; ``flag_symbols_detail`` says so.
  - Only function, method and class symbols are flagged. A hit that encloses another hit is dropped,
    so a changed method flags the method, not its class.
  - Public means no underscore-prefixed segment in the qualified name (an ``__init__`` module
    segment is ignored).
  - ``adjust`` vs ``net-new`` reads the sidecar of ``common_dir.parent``; for a linked worktree that
    is the main checkout, so kinds there degrade to ``net-new``.
  - A missing symtab, unreadable object or sqlite error yields an empty list plus a detail reason,
    never an exception.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from coordinator_core.git.commit_walk import commit_meta
from coordinator_core.git.git_objects import read_object
from coordinator_core.git.git_state import _parse_tree_entries
from coordinator_core.symbol_knowledge.contract import KIND_ADJUST, KIND_NET_NEW, FlaggedSymbol

SYMTAB_NAME = "scip_python.symtab.sqlite3"
FLAGGABLE_KINDS = ("function", "method", "class")
_TREE_MODE = 0o40000
_IN_CHUNK = 400
_SEGMENT_SPLIT = re.compile(r"[#./()\[\]:!]+")
_DESCRIPTOR = re.compile(r"`([^`]*)`/(.*)", re.S)

PathLike = Union[str, Path]
_Entries = Dict[str, Tuple[int, str]]


def symtab_path(graph_db: PathLike) -> Path:
    """The SCIP symtab beside *graph_db*."""
    return Path(graph_db).parent / SYMTAB_NAME


def is_public(scip_symbol: str) -> bool:
    """True when no segment of the symbol's module path or descriptor starts with an underscore."""
    parts = scip_symbol.split(" ", 4)
    match = _DESCRIPTOR.match(parts[4]) if len(parts) == 5 else None
    if match is None:
        return False
    module = [s for s in match.group(1).split(".") if s and s != "__init__"]
    rest = [s for s in _SEGMENT_SPLIT.split(match.group(2)) if s]
    return not any(seg.startswith("_") for seg in (*module, *rest))


def _tree_entries(common_dir: Path, sha: str) -> Optional[_Entries]:
    obj = read_object(common_dir, sha)
    if obj is None or obj[0] != "tree":
        return None
    return _parse_tree_entries(obj[1])


def _changed_py_blobs(
    common_dir: Path, old_tree: str, new_tree: str, prefix: str, out: "List[Tuple[str, str, str]]"
) -> bool:
    """Append ``(path, old_blob_sha, new_blob_sha)`` for each ``.py`` file that is in both trees with
    differing content; False when a tree object is unreadable."""
    if old_tree == new_tree:
        return True
    old = _tree_entries(common_dir, old_tree)
    new = _tree_entries(common_dir, new_tree)
    if old is None or new is None:
        return False
    for name, (new_mode, new_sha) in new.items():
        prior = old.get(name)
        if prior is None or prior[1] == new_sha:
            continue
        path = f"{prefix}{name}"
        if new_mode == _TREE_MODE and prior[0] == _TREE_MODE:
            if not _changed_py_blobs(common_dir, prior[1], new_sha, path + "/", out):
                return False
        elif new_mode != _TREE_MODE and prior[0] != _TREE_MODE and name.endswith(".py"):
            out.append((path, prior[1], new_sha))
    return True


def _blob(common_dir: Path, sha: str) -> Optional[bytes]:
    obj = read_object(common_dir, sha)
    return obj[1] if obj is not None and obj[0] == "blob" else None


def _old_byte_ranges(old: bytes, new: bytes) -> "List[Tuple[int, int]]":
    """Parent-side byte ranges of every differing hunk; an insertion is a zero-length range at its
    position."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    offsets = [0]
    for line in old_lines:
        offsets.append(offsets[-1] + len(line))
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    return [
        (offsets[i1], offsets[i2])
        for tag, i1, i2, _j1, _j2 in matcher.get_opcodes()
        if tag != "equal"
    ]


def _hits(ranges: "List[Tuple[int, int]]", start: int, end: int) -> bool:
    for lo, hi in ranges:
        if lo == hi:
            if start < lo <= end:
                return True
        elif lo < end and hi > start:
            return True
    return False


def _drop_enclosing(hits: "List[Tuple[str, int, int]]") -> "List[Tuple[str, int, int]]":
    kept = []
    for sym, start, end in hits:
        encloses = any(
            (start, end) != (s2, e2) and start <= s2 and e2 <= end for _sym, s2, e2 in hits
        )
        if not encloses:
            kept.append((sym, start, end))
    return kept


def _rows_by_file(db: sqlite3.Connection, paths: "List[str]") -> "Dict[str, List[Tuple[str, int, int]]]":
    kinds = ",".join("?" * len(FLAGGABLE_KINDS))
    rows: "Dict[str, List[Tuple[str, int, int]]]" = {}
    for i in range(0, len(paths), _IN_CHUNK):
        chunk = paths[i : i + _IN_CHUNK]
        marks = ",".join("?" * len(chunk))
        cursor = db.execute(
            f"SELECT file, scip_symbol, byte_start, byte_end FROM symtab "
            f"WHERE file IN ({marks}) AND kind IN ({kinds})",
            (*chunk, *FLAGGABLE_KINDS),
        )
        for file, sym, start, end in cursor:
            rows.setdefault(file, []).append((sym, start, end))
    return rows


def _known_names(common_dir: Path) -> "set[str]":
    from coordinator_core.symbol_knowledge.sidecar import read_sidecar

    if common_dir.name != ".git":
        return set()
    return set(read_sidecar(common_dir.parent))


def flag_symbols_detail(
    common_dir: PathLike, sha: str, graph_db: PathLike
) -> "Tuple[List[FlaggedSymbol], str]":
    """``flag_symbols`` plus a one-line detail naming why the list is empty or incomplete."""
    common = Path(common_dir)
    symtab = symtab_path(graph_db)
    if not symtab.is_file():
        return [], "SCIP symtab absent"
    meta = commit_meta(common, sha)
    if meta is None or not meta.get("tree"):
        return [], "commit unreadable"
    if not meta.get("parents"):
        return [], "root commit has no parent to diff"
    parent = commit_meta(common, meta["parents"][0])
    if parent is None or not parent.get("tree"):
        return [], "parent commit unreadable"
    changed: "List[Tuple[str, str, str]]" = []
    if not _changed_py_blobs(common, parent["tree"], meta["tree"], "", changed):
        return [], "tree object unreadable"
    if not changed:
        return [], "commit changes no existing python file"
    try:
        uri = "file:" + symtab.resolve().as_posix() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True)
        try:
            rows = _rows_by_file(db, [path for path, _o, _n in changed])
        finally:
            db.close()
    except sqlite3.Error as exc:
        return [], f"SCIP symtab unreadable: {type(exc).__name__}"
    known = _known_names(common)
    flagged: "List[FlaggedSymbol]" = []
    for path, old_sha, new_sha in changed:
        spans = [r for r in rows.get(path, ()) if is_public(r[0])]
        if not spans:
            continue
        old, new = _blob(common, old_sha), _blob(common, new_sha)
        if old is None or new is None:
            continue
        ranges = _old_byte_ranges(old, new)
        hits = _drop_enclosing([r for r in spans if _hits(ranges, r[1], r[2])])
        for sym, _start, _end in sorted(hits):
            kind = KIND_ADJUST if sym in known else KIND_NET_NEW
            flagged.append(FlaggedSymbol(sym, path, kind, True))
    if flagged:
        return flagged, f"{len(flagged)} public symbol(s) flagged; symbols the commit adds appear after the next reindex"
    return [], "no public symbol span overlaps a changed hunk; symbols the commit adds appear after the next reindex"


def flag_symbols(common_dir: PathLike, sha: str, graph_db: PathLike) -> "List[FlaggedSymbol]":
    """Public symbols whose parent-side spans the commit *sha* changed; 0 git spawns."""
    return flag_symbols_detail(common_dir, sha, graph_db)[0]
