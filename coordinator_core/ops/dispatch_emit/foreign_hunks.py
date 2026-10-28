"""Hunk headers a holder run's recorded edits carry on a path another run is about to land.

Zero spawns: objects are read through ``git_objects`` via the since-base-overlap readers. Stdlib
only at module top; callers import this module lazily (land_run cold-import pin).

Invariant: kept and worktree bytes are raw; base and HEAD blobs are checkin form. They meet only
through ``kept_work.checkin_form``; a refused lift contributes nothing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, Optional


class Headers(list):
    """Capped ``@@ -a,b +c,d @@`` headers; ``total`` is the uncapped count, for a "+N more" tail."""

    total: int = 0


def _span(start: int, end: int) -> str:
    n = end - start
    return f"{start if n == 0 else start + 1},{n}"


def _headers(a: List[bytes], b: List[bytes]) -> List[str]:
    from coordinator_core.git.since_base_overlap import _opcodes

    found = set()
    for tag, i1, i2, j1, j2 in _opcodes(a, b):
        if tag != "equal":
            found.add((i1, i2, j1, j2))
    return [f"@@ -{_span(i1, i2)} +{_span(j1, j2)} @@" for i1, i2, j1, j2 in sorted(found)]


def _attributed(root: Path, common_dir: Path, path: str, holder: str) -> List[str]:
    from coordinator_core.git.git_objects import read_object
    from coordinator_core.git.rollback_check import _blob_at_commit
    from coordinator_core.git.since_base_overlap import _lines
    from coordinator_core.ops.dispatch_emit import halted_run, kept_work
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    rdir = record_dir(root, holder)
    base = halted_run.halted_run_base(rdir)
    blobs = kept_work.kept_fingerprints(rdir).get(path)
    if not base or not blobs:
        return []
    base_lines = _lines(common_dir, _blob_at_commit(common_dir, base, path))
    if base_lines is None:
        return []
    out = set()
    for blob in blobs:
        obj = read_object(common_dir, blob)
        if obj is None:
            continue
        lifted = kept_work.checkin_form(root, path, obj[1])
        if lifted is not None:
            out.update(_headers(base_lines, lifted.splitlines(keepends=True)))
    return list(out)


def _unattributed(root: Path, common_dir: Path, path: str) -> List[str]:
    from coordinator_core.git.rollback_check import _blob_at_commit
    from coordinator_core.git.since_base_overlap import _head_sha, _lines

    head = _head_sha(root)
    if not head:
        return []
    head_lines = _lines(common_dir, _blob_at_commit(common_dir, head, path))
    if head_lines is None:
        return []
    from coordinator_core.ops.dispatch_emit import kept_work

    lifted = kept_work.checkin_form(root, path, (root / path).read_bytes())
    if lifted is None:
        return []
    return _headers(head_lines, lifted.splitlines(keepends=True))


def _sort_key(header: str):
    return tuple(int(n) for n in re.findall(r"\d+", header))


def foreign_hunks(
    worktree_root: Path, pairs: Iterable[tuple[str, str]], *, cap: int = 3
) -> list[tuple[str, str, list[str], bool]]:
    """``(path, holder_run_id, headers, attributed)`` per pair that yields any header.

    ``attributed`` means the headers were read from the holder's own record (``run_base_sha`` blob
    against its ``kept.json`` blobs): the hunks the holder's recorded blob carries, not proof the
    holder wrote them. Kept and worktree bytes are raw and are lifted with ``kept_work.checkin_form``
    before diffing against base/HEAD blobs (checkin form). Otherwise headers are HEAD against the
    worktree bytes on the path. Headers are capped at ``cap`` and carry the uncapped count as
    ``.total``. Never raises; an unreadable record, object or file contributes nothing for that
    pair."""
    out: list[tuple[str, str, list[str], bool]] = []
    try:
        root = Path(worktree_root)
        from coordinator_core.git.git_dir import resolve_git_common_dir

        common_dir = resolve_git_common_dir(root)
    except Exception:
        return out
    for path, holder in pairs:
        found: Optional[List[str]] = None
        attributed = False
        try:
            found = _attributed(root, common_dir, path, holder)
            attributed = bool(found)
        except Exception:
            found = []
        if not found:
            try:
                found = _unattributed(root, common_dir, path)
            except Exception:
                found = []
        if not found:
            continue
        ordered = sorted(set(found), key=_sort_key)
        headers = Headers(ordered[: max(cap, 0)])
        headers.total = len(ordered)
        out.append((path, holder, headers, attributed))
    return out
