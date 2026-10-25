"""May this run finish the merge that is open in its worktree, and with which paths.

Stdlib-only at the top: terminal_commit's cold-import pin covers this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple, Optional

FOREIGN_KEY = "foreign"


class RunOwnedMerge(NamedTuple):
    """Sorted repo-relative paths the open merge commit carries: still on disk, and gone from disk."""

    present: tuple[str, ...]
    deleted: tuple[str, ...]


def run_owned_merge(worktree_root: Path, run_id: Optional[str]) -> Optional[RunOwnedMerge]:
    """The open merge's changed paths when ``run_id`` started it, else None (fail closed).

    Trap: the set is the merge's changed paths, i.e. exactly what ``landing_plan`` hands the
    merge commit; never the worktree's dirt. No spawn when ``run_id`` is falsy or no merge is open."""
    if not run_id:
        return None
    from coordinator_core.git import merge_owner, merge_state
    from coordinator_core.git.git_dir import resolve_git_dir

    if not (resolve_git_dir(worktree_root) / "MERGE_HEAD").exists():
        return None
    if merge_owner.owner(worktree_root) != run_id:
        return None
    try:
        state = merge_state.read_merge_state(worktree_root)
    except merge_state.MergeStateError:
        return None
    if state is None:
        return None
    merge_set = set(state.merge_paths)
    gone = sorted(p for p in merge_set if not (worktree_root / p).exists())
    return RunOwnedMerge(tuple(sorted(merge_set - set(gone))), tuple(gone))


def landing_plan(
    worktree_root: Path, all_paths: list, deleted_paths: list, *, product: set,
    owner_run_id: Optional[str] = None,
) -> Optional[dict]:
    """None when no merge is open. Otherwise ``{"refusal", "extra"}`` when the merge may not
    finish (markers, unresolved paths, foreign declared paths), else the merge commit's
    ``paths``/``deleted`` (exactly the merge's changed paths) and the run artifacts
    (``artifacts``) that land afterwards in their own single-parent commit.

    With ``owner_run_id`` and an open merge that run did not start (no record counts as
    foreign), returns ``{FOREIGN_KEY: True, "merge_head": <live sha or None>}`` and reads
    nothing further."""
    from coordinator_core.git import merge_owner, merge_state
    from coordinator_core.git.git_dir import resolve_git_dir

    head_file = resolve_git_dir(worktree_root) / "MERGE_HEAD"
    if not head_file.exists():
        return None
    if owner_run_id is not None and merge_owner.owner(worktree_root) != owner_run_id:
        try:
            live = head_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            live = None
        return {FOREIGN_KEY: True, "merge_head": live}
    try:
        state = merge_state.read_merge_state(worktree_root)
    except merge_state.MergeStateError as exc:
        return {"refusal": f"merge state unreadable: {exc}", "extra": {"refused": "merge_unreadable"}}
    if state is None:
        return None
    declared = [p for p in (*all_paths, *deleted_paths) if p in product]
    refusal = merge_state.merge_refusal(
        state, sorted({*declared, *state.merge_paths}), worktree_root
    )
    if refusal is not None:
        return {
            "refusal": f"merge not finished, HEAD unmoved: {refusal}",
            "extra": {"refused": refusal.split(":", 1)[0], "merge_paths": list(state.merge_paths)},
        }
    merge_set = set(state.merge_paths)
    gone = sorted(p for p in merge_set if not (worktree_root / p).exists())
    return {
        "paths": sorted(merge_set - set(gone)),
        "deleted": gone,
        "artifacts": {
            "paths": [p for p in all_paths if p not in merge_set],
            "deleted": [p for p in deleted_paths if p not in merge_set],
        },
    }
