"""coordinator_core.run_watermark -- the commit-gated coverage watermark a distill or
learn-lessons run reads its delta against.

Purpose: one JSON per run kind at ``state/run-watermarks/<kind>.json`` holds a pass base
(git sha plus, when example-retrieval-repo's read-contract view is present, an ``index_generation``)
and a cursor through the sorted backlog. A run scopes the files changed since the base plus
the next backlog slice, stages a proposed watermark in its run dir (``stage``), and only the
run's own commit point calls ``promote``, so an uncommitted run never advances coverage.

Delta source, in order: the example-retrieval-repo ``doc_files`` view (settings key
``paths.project_rag_doc_files_db``, opened ``mode=ro``, no process spawn); when the key is
unset or the db unreadable, ONE ``git diff --name-only <base_sha>`` through the shared git
runner. Never an MCP or CLI spawn.

Invariants: ``candidates_sorted`` handed to ``select`` ascends in ``str`` order of the same
strings the cursor holds (date-prefixed names sort that way); the cursor is the last
candidate a run covered. The base stays pinned for a whole incomplete pass and moves to
HEAD / the current generation on every run once ``pass_complete``.
"""

from __future__ import annotations

import bisect
import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.git.run import git_out
from coordinator_core.machine_resolver import registry_get
from coordinator_core.session.claimed_write import replace_bytes

DOC_FILES_DB_KEY = "paths.project_rag_doc_files_db"
WATERMARK_DIR_REL = "state/run-watermarks"
PENDING_NAME = "watermark.pending.json"

_GENERATION_QUERIES = (
    "SELECT MAX(index_generation) FROM generation",
    "SELECT MAX(index_generation) FROM doc_files",
)


class ChangedSinceError(RuntimeError):
    """No delta source could answer (git failed and no readable doc_files view)."""


@dataclass(frozen=True)
class Watermark:
    """``cursor`` is the last covered candidate (None before any coverage)."""

    kind: str
    base_sha: str
    base_generation: Optional[int] = None
    cursor: Optional[str] = None
    pass_complete: bool = False


class ChangedSet(set):
    """Changed paths plus ``generation``: the generation read in the same connection
    (None when no db answered), which ``select`` proposes as the next base_generation."""

    generation: Optional[int] = None


def watermark_path(repo_root: Path, kind: str) -> Path:
    return Path(repo_root) / WATERMARK_DIR_REL / f"{kind}.json"


def _from_dict(data: object, kind: str) -> Optional[Watermark]:
    if not isinstance(data, dict) or not isinstance(data.get("base_sha"), str):
        return None
    gen = data.get("base_generation")
    cursor = data.get("cursor")
    if gen is not None and (isinstance(gen, bool) or not isinstance(gen, int)):
        return None
    if cursor is not None and not isinstance(cursor, str):
        return None
    return Watermark(
        kind=kind,
        base_sha=data["base_sha"],
        base_generation=gen,
        cursor=cursor,
        pass_complete=data.get("pass_complete") is True,
    )


def load(repo_root: Path, kind: str) -> Optional[Watermark]:
    """The committed watermark for ``kind``; None when absent or unreadable (first run)."""
    try:
        data = json.loads(watermark_path(repo_root, kind).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _from_dict(data, kind)


def _under(path: str, scope_dirs: Sequence[str]) -> bool:
    p = path.replace("\\", "/")
    for d in scope_dirs:
        d = d.replace("\\", "/").strip("/")
        if d in ("", "."):
            return True
        if p == d or p.startswith(d + "/"):
            return True
    return False


def _rag_changed(db: str, wm: Watermark, scope_dirs: Sequence[str]) -> ChangedSet:
    conn = sqlite3.connect(Path(db).as_uri() + "?mode=ro", uri=True)
    try:
        if wm.base_generation is not None:
            rows = conn.execute(
                "SELECT path FROM doc_files WHERE index_generation > ?",
                (wm.base_generation,),
            )
        else:
            rows = conn.execute(
                "SELECT path FROM doc_files WHERE indexed_commit IS NOT ?", (wm.base_sha,)
            )
        changed = ChangedSet(r[0] for r in rows if _under(r[0], scope_dirs))
        changed.generation = _generation(conn)
        return changed
    finally:
        conn.close()


def _generation(conn: sqlite3.Connection) -> Optional[int]:
    for q in _GENERATION_QUERIES:
        try:
            value = conn.execute(q).fetchone()
        except sqlite3.Error:
            continue
        if value and isinstance(value[0], int):
            return value[0]
    return None


def _read_generation(db: str) -> Optional[int]:
    try:
        conn = sqlite3.connect(Path(db).as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        return _generation(conn)
    finally:
        conn.close()


def changed_since(repo_root: Path, wm: Optional[Watermark], *, scope_dirs: Sequence[str]) -> ChangedSet:
    """Paths under ``scope_dirs`` changed since ``wm``'s base; empty for a first run (wm None)."""
    db = registry_get(DOC_FILES_DB_KEY)
    if wm is None:
        empty = ChangedSet()
        if db:
            empty.generation = _read_generation(db)
        return empty
    if db:
        try:
            return _rag_changed(db, wm, scope_dirs)
        except sqlite3.Error:
            pass
    argv = ["-C", str(repo_root), "diff", "--name-only", wm.base_sha]
    if scope_dirs:
        argv += ["--", *scope_dirs]
    out = git_out(argv)
    if out is None:
        raise ChangedSinceError(f"git diff --name-only {wm.base_sha} failed")
    return ChangedSet(line for line in out.splitlines() if line)


def select(
    candidates_sorted: Sequence[str],
    wm: Optional[Watermark],
    changed: ChangedSet,
    budget_files: int,
    *,
    kind: str = "",
    head_sha: Optional[str] = None,
) -> tuple[set[str], Watermark]:
    """Scoped files (changed ∩ candidates, plus the next ``budget_files`` after the cursor)
    and the proposed watermark. ``head_sha`` is required on a first run (it pins the pass
    base) and whenever the pass completes."""
    start = bisect.bisect_right(candidates_sorted, wm.cursor) if wm and wm.cursor else 0
    slice_ = list(candidates_sorted[start : start + max(budget_files, 0)])
    scoped = set(slice_) | (set(changed) & set(candidates_sorted))
    done = start + len(slice_) >= len(candidates_sorted)
    cursor = slice_[-1] if slice_ else (wm.cursor if wm else None)
    if wm is None:
        if head_sha is None:
            raise ValueError("head_sha is required on a first run")
        base = Watermark(kind, head_sha, changed.generation)
    else:
        base = wm
    complete = done or base.pass_complete
    if complete:
        if head_sha is None:
            raise ValueError("head_sha is required to advance a completed pass")
        base = replace(base, base_sha=head_sha, base_generation=changed.generation)
    return scoped, replace(base, cursor=cursor, pass_complete=complete)


def stage(run_dir: Path, proposed: Watermark) -> Path:
    """Write the proposed watermark into the run dir; nothing is committed yet."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    body = json.dumps(asdict(proposed), indent=2, sort_keys=True) + "\n"
    return replace_bytes(run_dir / PENDING_NAME, body.encode("utf-8"))


def promote(repo_root: Path, run_dir: Path, kind: str) -> Optional[Path]:
    """Copy the run's pending watermark into state; the returned path joins the caller's
    commit pathspec. No (valid) pending -> None, nothing written."""
    pending = Path(run_dir) / PENDING_NAME
    try:
        data = json.loads(pending.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    wm = _from_dict(data, kind)
    if wm is None:
        return None
    target = watermark_path(repo_root, kind)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(asdict(wm), indent=2, sort_keys=True) + "\n"
    return replace_bytes(target, body.encode("utf-8"))
