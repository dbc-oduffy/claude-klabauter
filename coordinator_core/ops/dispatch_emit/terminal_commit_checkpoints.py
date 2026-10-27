"""Checkpoint-commit attribution for dispatch.terminal_commit: predecessor and halted-run checkpoint rows."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence, Optional
from coordinator_core.ops.dispatch_emit.checkpoint_stamp import (
    is_stamp_commit,
    CHECKPOINT_PLAN_LINE,
    CHECKPOINT_SUBJECT,
)
from coordinator_core.ops.dispatch_emit.plan_row_coding import (
    yaml_load as _yaml_load,
    source_rows_by_plan as _source_rows_by_plan,
)
from coordinator_core.ops.dispatch_emit.worktree_snapshot import read_rel as _read_rel


_LANDS_CHUNKS_LINE = re.compile(r"^Lands-Chunks:[ \t]*(?P<v>.+?)\s*$", re.M)

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _predecessor_rows(worktree_root: Path, plan_path: Optional[str], resume: object) -> dict:
    """``{halt sha: {plan: {row id}}}`` of the rows a ledger resume's halt commit landed.

    Reads the one commit ``resume_ledger.halt_sha`` names, in-process (zero spawns), and maps
    its ``Lands-Chunks:`` ids to plan rows. Empty for a non-resume close, a bad sha or an
    unreadable commit.
    """
    halt_sha = resume.get("halt_sha") if isinstance(resume, dict) else None
    if not plan_path or not isinstance(halt_sha, str) or not _FULL_SHA_RE.fullmatch(halt_sha):
        return {}
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir

    meta = commit_meta(resolve_git_common_dir(worktree_root), halt_sha)
    message = (meta or {}).get("message") or ""
    ids = sorted({
        i for m in _LANDS_CHUNKS_LINE.finditer(message)
        for i in re.split(r"[,\s]+", m.group("v")) if i
    })
    by_plan = _source_rows_by_plan(worktree_root, plan_path, ids)
    return {halt_sha: by_plan} if by_plan else {}


_HALTED_WALK_CAP = 500


def _checkpoint_rows_from(
    worktree_root: Path, plan_path: Optional[str], start_sha: object, exclude: frozenset = frozenset(),
) -> dict:
    """``{checkpoint sha: {plan: {row id}}}`` of the still-open rows same-plan checkpoints landed.

    One in-process walk (``checkpoint_stamp.checkpoint_rows_landed``, zero spawns) from
    ``start_sha`` for the plan's rows open on disk minus ``exclude``; each row maps to the
    newest checkpoint that named it. ``Checkpoint-Base:`` is not a stop: the wanted set, the
    plan's ``created:`` floor and ``_HALTED_WALK_CAP`` bound it. Empty for a bad sha or a plan
    with no open row.
    """
    if not plan_path or not isinstance(start_sha, str) or not _FULL_SHA_RE.fullmatch(start_sha):
        return {}
    wanted = frozenset(_open_row_ids(worktree_root, plan_path) - set(exclude))
    if not wanted:
        return {}
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import checkpoint_rows_landed, plan_created_floor
    from coordinator_core.ops.dispatch_emit import terminal_commit as _tc

    walk = checkpoint_rows_landed(
        worktree_root, plan_path, start_sha, wanted,
        floor_epoch=plan_created_floor(_read_rel(worktree_root, plan_path) or ""),
        cap=_tc._HALTED_WALK_CAP,
    )
    landed: dict = {}
    for sha, ids in walk.landed.items():
        by_plan = _source_rows_by_plan(worktree_root, plan_path, sorted(ids))
        if by_plan:
            landed[sha] = by_plan
    return landed


def _plan_checkpoint_touched(worktree_root: Path, plan_path: Optional[str], paths: Sequence[str]) -> frozenset:
    """The ``paths`` whose blob a same-plan checkpoint landing an open row changed. Unlike
    ``own_checkpoint_paths.checkpoint_landed_paths`` the walk steps over non-checkpoint commits (a
    halt's run-record commit sits above the checkpoints on a refired run), bounded by the open rows
    and the plan's ``created:`` floor."""
    wanted_paths = [p.replace("\\", "/") for p in paths if isinstance(p, str) and p]
    if not plan_path or not wanted_paths:
        return frozenset()
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_state import head_sha
    from coordinator_core.git.rollback_check import ABSENT, _blob_at_commit
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import (
        _walk_commit_meta, checkpoint_rows_landed, plan_created_floor,
    )
    from coordinator_core.ops.dispatch_emit import terminal_commit as _tc

    head =head_sha(worktree_root)
    rows = _open_row_ids(worktree_root, plan_path)
    if not head or not rows:
        return frozenset()
    walk = checkpoint_rows_landed(
        worktree_root, plan_path, head, frozenset(rows),
        floor_epoch=plan_created_floor(_read_rel(worktree_root, plan_path) or ""),
        cap=_tc._HALTED_WALK_CAP,
    )
    common_dir = resolve_git_common_dir(Path(worktree_root))
    touched: set = set()
    for sha in walk.landed:
        meta = _walk_commit_meta(common_dir, sha)
        parents = (meta or {}).get("parents") or []
        for p in wanted_paths:
            cur = _blob_at_commit(common_dir, sha, p)
            prev = ABSENT if not parents else _blob_at_commit(common_dir, parents[0], p)
            if cur is not None and prev is not None and cur != prev:
                touched.add(p)
    return frozenset(touched)


def _halted_run_checkpoint_rows(
    worktree_root: Path, plan_path: Optional[str], resume: object, exclude: frozenset = frozenset(),
) -> dict:
    """``_checkpoint_rows_from`` the ledger resume's ``halt_sha``; empty for a non-resume close."""
    halt_sha = resume.get("halt_sha") if isinstance(resume, dict) else None
    return _checkpoint_rows_from(worktree_root, plan_path, halt_sha, exclude)


def _earlier_fire_checkpoint_rows(
    worktree_root: Path, plan_path: Optional[str], terminal_sha: str, exclude: frozenset = frozenset(),
) -> dict:
    """``_checkpoint_rows_from`` the terminal commit's first parent: a non-resume close's earlier fires."""
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir

    meta = commit_meta(resolve_git_common_dir(worktree_root), terminal_sha)
    parents = (meta or {}).get("parents") or []
    return _checkpoint_rows_from(worktree_root, plan_path, parents[0] if parents else None, exclude)


def _checkpoint_attribution(worktree_root: Path, terminal_sha: str, plan_path: Optional[str], chunks: list) -> dict:
    """Chunk id -> sha of the run's own ``checkpoint(wave N)`` commit that landed its writes.

    Walks first-parent history in-process (zero spawns) from the terminal commit's
    parent while the subject is a checkpoint of this plan (``Checkpoint-Plan:``
    trailer), so the walk is bounded by the run. A commit lands a chunk's file when
    its blob differs from its parent's. Among the commits touching a chunk, one whose
    subject names the chunk id wins; otherwise the earliest. A chunk no checkpoint
    touched is absent: it stays a no-op row.
    """
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.rollback_check import _blob_at_commit
    from coordinator_core.ops.dispatch_emit import terminal_commit as _tc

    common_dir = resolve_git_common_dir(worktree_root)
    meta = commit_meta(common_dir, terminal_sha)
    parents = (meta or {}).get("parents") or []
    sha = parents[0] if parents else None
    checkpoints: list = []  # newest first: (sha, parent, named ids)
    for _ in range(_tc._HALTED_WALK_CAP):
        meta = commit_meta(common_dir, sha) if sha else None
        if meta is None:
            break
        message = meta.get("message") or ""
        if is_stamp_commit(message) and meta.get("parents"):
            sha = meta["parents"][0]
            continue
        subject = CHECKPOINT_SUBJECT.match((message.splitlines() or [""])[0].strip())
        plan = CHECKPOINT_PLAN_LINE.search(message)
        if not subject or not meta.get("parents") or (plan_path and (not plan or plan.group("v") != plan_path)):
            break
        named = {i.strip() for i in subject.group("ids").split(",") if i.strip()}
        checkpoints.append((sha, meta["parents"][0], named))
        sha = meta["parents"][0]
    out: dict = {}
    for chunk in chunks:
        hits = [
            (csha, named) for csha, parent, named in reversed(checkpoints)
            if any(_blob_at_commit(common_dir, csha, p) != _blob_at_commit(common_dir, parent, p) for p in chunk.paths)
        ]
        if hits:
            out[chunk.id] = next((h[0] for h in hits if chunk.id in h[1]), hits[0][0])
    return out


def _open_row_ids(worktree_root: Path, plan_rel: str) -> set:
    """Ids of the plan spine rows whose disposition is ``open`` on disk now."""
    import yaml

    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
    from coordinator_core.execute_plan_assemble.row_spans import _row_disposition

    text = _read_rel(worktree_root, plan_rel)
    if text is None:
        return set()
    located = locate_fenced_block(text.replace("\r\n", "\n"))
    if located.status != LocateStatus.LOCATED:
        return set()
    try:
        rows = _yaml_load(located.body) or []
    except yaml.YAMLError:
        return set()
    if not isinstance(rows, list):
        return set()
    return {
        str(r.get("id")) for r in rows
        if isinstance(r, dict) and _row_disposition(r) == "open"
    }

