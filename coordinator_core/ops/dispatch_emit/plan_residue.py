"""The paths a run's plan declares that a dead pass left edited and unrecorded.

Invariant: residue is derived from the tree and git history alone, never from a record the dead
pass wrote. A path is residue only when the run's write set (declared plus credited rows'
writes, minus peer-held and scratch) names it and its worktree blob differs from both the newest
same-plan checkpoint's blob and HEAD's. Spawns nothing.
"""

from __future__ import annotations

from pathlib import Path


def _plan_row_ids(root: Path, plan_rel: str) -> frozenset:
    from coordinator_core.frontmatter.body_blocks import LocateStatus
    from coordinator_core.ops.dispatch_emit.spine_read import load_rows_memo

    try:
        text = (root / plan_rel).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return frozenset()
    loaded = load_rows_memo(text)
    if loaded.status is not LocateStatus.LOCATED:
        return frozenset()
    return frozenset(str(r["id"]) for r in loaded.rows if isinstance(r, dict) and r.get("id"))


def _checkpoint_sha(root: Path, plan_rel: str, head: str) -> str:
    """Newest same-plan checkpoint over any of the plan's rows; HEAD when none is found."""
    from coordinator_core.ops.dispatch_emit import terminal_commit as _tc
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import checkpoint_rows_landed, plan_created_floor

    rows = _plan_row_ids(root, plan_rel)
    if not rows:
        return head
    try:
        floor = plan_created_floor((root / plan_rel).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        floor = None
    walk = checkpoint_rows_landed(root, plan_rel, head, rows, floor_epoch=floor, cap=_tc._HALTED_WALK_CAP)
    return next(iter(walk.landed), head)


def own_plan_residue(root: Path, run_dir: Path, own_session: str = "") -> frozenset:
    """Repo-relative POSIX paths the run's write set names whose worktree blob differs from the
    last same-plan checkpoint's and from HEAD's. Empty when the run record, plan or HEAD is
    unreadable; a path whose checkpoint blob is unreadable is skipped (cannot be compared)."""
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_state import head_sha
    from coordinator_core.git.rollback_check import ABSENT, _blob_at_commit
    from coordinator_core.ops.dispatch_emit import land_run
    from coordinator_core.ops.dispatch_emit import run_write_set as rws
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    root = Path(root)
    run_id = Path(run_dir).name
    manifest = rws._json_object(record_dir(root, run_id) / "manifest.json")
    if manifest is None:
        return frozenset()
    plan_rel = rws._plan_rel(root, manifest, rws._sizing_rel(record_dir(root, run_id)))
    head = head_sha(root)
    declared = rws.run_write_set(root, run_id, own_session)
    if not plan_rel or not head or not declared:
        return frozenset()
    sha = _checkpoint_sha(root, plan_rel, head)
    common_dir = resolve_git_common_dir(root)

    def at(commit: str, path: str):
        blob = _blob_at_commit(common_dir, commit, path)
        return None if blob is ABSENT else blob

    out: set = set()
    for path in declared:
        if _blob_at_commit(common_dir, sha, path) is None or _blob_at_commit(common_dir, head, path) is None:
            continue
        wt = land_run._worktree_blob(root, path)
        if wt != at(sha, path) and wt != at(head, path):
            out.add(path)
    return frozenset(out)
