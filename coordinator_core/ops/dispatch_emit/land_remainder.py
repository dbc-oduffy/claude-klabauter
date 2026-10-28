"""``dispatch.land_verified_remainder``: resume a halted run at its commit step and commit its verified
remainder under the run's id.

Order: refuse a run that is not halted, check the run token (only checked, never spent, as
``land_run_halt`` does), refuse requested paths outside the run's declared writes, verify the on-disk
bytes (import closure inline, retest from the detached child), then commit through
``git.commit.commit_paths`` with ``Run-Id:`` and ``Lands-Chunks:`` trailers. Declared dirty paths the
commit leaves out are named in the reply's ``omitted``.
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.dispatch_emit import land_remainder_contract as contract
from coordinator_core.ops.dispatch_emit import land_remainder_verify as verify
from coordinator_core.ops.dispatch_emit import land_remainder_writeset as writeset


def _reply(**extra: object) -> dict:
    return {"landed": False, "sha": None, "refused": None, "omitted": [], "closure_open": [], "retest": None, **extra}


def _refusal(code: str, error: str, **extra: object) -> dict:
    return _reply(refused=code, error=error, **extra)


def _halted_record(root: Path, run_id: str) -> Optional[dict]:
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    try:
        data = json.loads((record_dir(root, run_id) / "halted.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("halted") else None


def _landed_chunks(root: Path, run_id: str, committed: set, omitted: set) -> list[str]:
    """Spine row ids with a committed declared path and no declared path left out of the commit."""
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
    from coordinator_core.ops.dispatch_emit.pathspec import _declared_paths
    from coordinator_core.ops.dispatch_emit.run_write_set import _plan_rel, _sizing_rel
    from coordinator_core.ops.dispatch_emit.spine_read import read_spine

    run_dir = record_dir(root, run_id)
    plan_rel = _plan_rel(root, writeset.manifest(run_dir), _sizing_rel(run_dir))
    if not plan_rel:
        return []
    out: list[str] = []
    for row in read_spine(root / plan_rel):
        declared = {p.replace("\\", "/") for p in _declared_paths(row)}
        if declared & committed and not declared & omitted:
            out.append(row.id)
    return sorted(out)


def _message(run_id: str, chunks: list[str], deleted: list[str]) -> str:
    lines = [f"warp: land verified remainder of {run_id}", "", f"Run-Id: {run_id}"]
    if chunks:
        lines.append("Lands-Chunks: " + ", ".join(chunks))
    if deleted:
        lines.append("Removes: " + ", ".join(deleted))
    return "\n".join(lines)


def land(root: Path, params: dict) -> dict:
    """Run the lane against worktree ``root``; the reply carries ``contract.REPLY_KEYS``."""
    from coordinator_core.ops._path_guard import safe_id

    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not safe_id(run_id):
        return _refusal("run-id-unsafe", f"params.run_id {run_id!r} is not a single safe path segment")
    halted = _halted_record(root, run_id)
    if halted is None:
        return _refusal(contract.REFUSED_NOT_HALTED, f"run {run_id!r} has no halt record; the lane resumes a halted run only")
    token = params.get("run_token")
    if token is not None:
        from coordinator_core.ops.dispatch_emit import run_token

        if not run_token.admits(root, run_id, token, params.get("script_path")):
            return _refusal("run-token", "params.run_token does not match a live, unspent token for params.run_id and this script")

    requested = params.get("paths")
    try:
        if isinstance(requested, list) and requested:
            outside = writeset.refuse_out_of_write_set(root, run_id, [str(p) for p in requested])
            if outside:
                return _refusal(
                    contract.REFUSED_OUT_OF_WRITE_SET,
                    "paths outside the run's declared writes: " + ", ".join(outside),
                    out_of_write_set=outside,
                )
        to_stage, omitted = writeset.dirty_declared(root, run_id)
    except ValueError as exc:
        return _refusal("no-write-set", str(exc))
    except writeset.DirtyWriteSetError as exc:
        return _refusal("dirty-check-failed", str(exc))
    if isinstance(requested, list) and requested:
        wanted = {str(p).replace("\\", "/") for p in requested}
        omitted = omitted + [(p, "not-requested") for p in to_stage if p not in wanted]
        to_stage = [p for p in to_stage if p in wanted]
    omitted_reply = [{"path": p, "reason": r} for p, r in omitted]
    if not to_stage:
        return _reply(omitted=omitted_reply, nothing_to_commit=True)

    verdict = verify.verify_remainder(root, run_id, to_stage)
    if verdict.get("refused"):
        return _reply(
            refused=verdict["refused"], omitted=omitted_reply,
            closure_open=verdict.get("closure_open") or [], retest=verdict.get("retest"),
        )

    paths = [p for p in to_stage if (root / p).is_file()]
    deleted = [p for p in to_stage if p not in set(paths)]
    chunks = _landed_chunks(root, run_id, set(to_stage), {p for p, _ in omitted})
    from coordinator_core.git.commit import (
        CommitRefused,
        NothingToCommit,
        commit_paths,
        hash_worktree_blobs_via_spawn,
    )

    try:
        outcome = commit_paths(
            root, paths, _message(run_id, chunks, deleted), deleted_paths=deleted, declared_reverts=deleted,
            blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=root),
        )
    except NothingToCommit:
        return _reply(omitted=omitted_reply, retest=verdict.get("retest"), nothing_to_commit=True)
    except CommitRefused as exc:
        return _refusal("commit-refused", str(exc), omitted=omitted_reply, retest=verdict.get("retest"))
    return _reply(
        landed=True, sha=outcome.sha, omitted=omitted_reply, retest=verdict.get("retest"),
        paths=sorted(paths + deleted), landed_chunks=chunks,
    )


@register_op("dispatch.land_verified_remainder")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Land a halted run's verified remainder: params ``run_id`` (required), optional ``paths``,
    ``run_token`` and ``script_path``; reply keys are ``land_remainder_contract.REPLY_KEYS``."""
    if repo_root is None:
        return _refusal("no-repo-root", f"{contract.OP_NAME} requires a common_dir-keyed dispatch; repo_root was not supplied")
    from coordinator_core.git.call_io_scope import call_io_scope
    from coordinator_core.lifecycle import main_worktree_root

    with call_io_scope():
        return land(main_worktree_root(repo_root), params)
