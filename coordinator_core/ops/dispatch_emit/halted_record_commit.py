"""The halted-run record-only commit: ``dispatch.terminal_commit`` given ``halted_by``.

A halted engine-ask run leaves its ``state/warp/<run_id>/`` record directory behind; this lands it
as one ``ceremony.commit_v2`` call carrying only that directory's top-level files and the halt
reason in the message. Nothing else is staged, no receipt, sizing ship, plan stamp or close job
runs, and the run token is redeemed then always released: a halt never spends the token the
resumed run's product commit needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    halted_record_message,
)


def is_halted_record_call(params: object) -> bool:
    """True when ``params[HALTED_RECORD_PARAM]`` is a dict with a non-empty str ``halted``."""
    halted = params.get(HALTED_RECORD_PARAM) if isinstance(params, dict) else None
    return isinstance(halted, dict) and isinstance(halted.get("halted"), str) and bool(halted["halted"])


def record_dir_changes(worktree_root: Path, record_dir_rel: str, changed: set) -> tuple[list, list]:
    """The record dir's changed top-level files from one status read: ``(present, deleted)``.
    ``present`` are files on disk; ``deleted`` are changed entries gone from disk."""
    prefix = record_dir_rel.rstrip("/") + "/"
    present: list = []
    deleted: list = []
    for rel in sorted(changed):
        if not rel.startswith(prefix) or "/" in rel[len(prefix):]:
            continue
        (present if (worktree_root / rel).is_file() else deleted).append(rel)
    return present, deleted


def commit_halted_record(worktree_root: Path, repo_root: Path, params: dict) -> dict:
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.terminal_commit import _error

    halted = params[HALTED_RECORD_PARAM]
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not safe_id(run_id):
        return _error(f"params.run_id {run_id!r} is not a single safe path segment", refused="run-id-unsafe")
    if contained_path(worktree_root / RECORD_DIR_ROOT / run_id, [worktree_root / RECORD_DIR_ROOT]) is None:
        return _error(f"record dir for {run_id!r} escapes {RECORD_DIR_ROOT}", refused="record-dir-escapes")
    record_dir_rel = f"{RECORD_DIR_ROOT}/{run_id}"

    token = params.get("run_token")
    script_path = params.get("script_path")
    if token is not None and not run_token.redeem(worktree_root, run_id, token, script_path):
        return _error(
            "params.run_token does not match a live, unspent token minted for params.run_id and "
            "this script (a token admits one landed commit)",
            refused="run-token",
        )
    try:
        return _land(worktree_root, repo_root, params, run_id, record_dir_rel, halted)
    finally:
        if token is not None:
            run_token.release(worktree_root, run_id, token, script_path)


def _land(
    worktree_root: Path, repo_root: Path, params: dict, run_id: str, record_dir_rel: str, halted: dict
) -> dict:
    from coordinator_core.git.commit_trailers import _UUID_RE
    from coordinator_core.ops.dispatch_emit.terminal_commit import _error, _worktree_snapshot
    from coordinator_core.warm.entry_seam import OpUnavailableError, reentrant_dispatch

    base = {"record_dir": record_dir_rel, "halted": halted["halted"]}
    snapshot = _worktree_snapshot(worktree_root, [], [record_dir_rel], [])
    if snapshot is None:
        return _error(f"git status over {record_dir_rel} failed", **base)
    paths, deleted = record_dir_changes(worktree_root, record_dir_rel, snapshot.changed)
    if not paths and not deleted:
        return {"committed": False, "sha": None, "nothing_to_commit": True, **base}

    message = halted_record_message(run_id, halted)
    if deleted:
        message += "\nRemoves: " + ", ".join(deleted)
    commit_params: dict = {"paths": paths, "message": message}
    if deleted:
        commit_params["deleted_paths"] = deleted
        commit_params["declared_reverts"] = deleted
    session_id: Optional[str] = params.get("session_id")
    if isinstance(session_id, str) and _UUID_RE.fullmatch(session_id):
        commit_params["session_id"] = session_id
    try:
        reply = reentrant_dispatch("ceremony.commit_v2", commit_params, repo_root=repo_root)
    except OpUnavailableError:
        return _error("ceremony.commit_v2 is not registered", **base)
    if not isinstance(reply, dict):
        return _error(f"unexpected commit_v2 reply: {reply!r}", **base)
    return {**reply, "paths": paths + deleted, **base}  # paths: everything the commit names
