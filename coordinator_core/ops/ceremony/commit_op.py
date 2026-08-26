"""
coordinator_core.ops.ceremony.commit_op — JSON-RPC "ceremony.commit" op.

Purpose: register a reachable, correctly-classified commit op so the warm
engine has a method to dispatch a commit to at all. C1 of
docs/plans/2026-08-26-the-commit-becomes-a-warm-served-op.md — the thinnest
useful version: this handler delegates to `run_commit_pipeline` UNCHANGED, no
git-mechanism work here. Later chunks (C3 onward) replace the pipeline's own
two-git-invocation agree branch with an in-process object write; this chunk's
only job is to make the seam REACHABLE (AC1) and correctly CLASSIFIED (AC2)
so every later chunk is measured through the real warm transport instead of
being exercised by calling `run_commit_pipeline` directly in a test.

Op name: `ceremony.commit` (plan § "The op's name chooses its budget") — a
FRESH decision, not a resurrection of the killed `ceremony.scoped_git_commit`
(DR-344: rebuild-from-scratch is the norm, never the old implementation made
faster). The `ceremony.` prefix is deliberate: it is both a
`_CEREMONY_PACKAGE_ALIASES` name match AND an `_owning_module_is_ceremony`
module-path match (this module lives under `ops/ceremony/`), so
`ipc.py :: _timeout_for` clamps it to `CEREMONY_BUDGET_SECS = 2.0` by
construction — the plan records and accepts that ceiling rather than adding
an override row (none exists for this op, and DR-348's ratchet forbids
raising the ceiling regardless).

Classification: MUTATING, asserted explicitly in `authz/classification.py`
rather than left to `_op_may_mutate`'s fail-closed default (AC2) — the
default is a safety net, not a declaration, and this op unambiguously writes
git objects/refs.

Keying scope: common_dir — same precedent as `commit.exec_bit_change`
(`ops/ceremony/commit_exec_bit.py`): the handler receives `repo_root` as the
git common dir and derives the caller's worktree via
`main_worktree_root(repo_root)`. `params.repo_root` is the optional D3
consistency assertion only (`check_repo_root`), never the worktree-resolution
source.

Spec backlinks:
    docs/plans/2026-08-26-the-commit-becomes-a-warm-served-op.md § C1, AC1, AC2

Negative-spec (hard-won, this chunk):
  - Does NOT reimplement or alter any git mechanism — `run_commit_pipeline`
    is called unchanged; C3 is the chunk that replaces its git-invocation
    shape with the in-process object write.
  - Does NOT resurrect the killed `ceremony.scoped_git_commit` op name or any
    string-keyed reference to it — this op has its own name, checked against
    the guard suites that caught a killed op's name living on before.
  - Does NOT support `stage_patch` (Out of scope: "stage_patch's private-index
    branch") or `on_committed` (no wire representation for a callback) — both
    are left at `run_commit_pipeline`'s own defaults.
  - Does NOT push — `push_mode` defaults to `PUSH_MODE_SYNC` (the pipeline's
    own untouched default) only when a caller explicitly asks for it; this
    chunk does no push-mode policy of its own beyond passing the param
    through. The push leg itself is Out of scope for the whole plan.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.commit_pipeline import run_commit_pipeline
from coordinator_core.ops.fleet._common import check_repo_root, main_worktree_root


def _error(message: str, **extra: object) -> dict:
    """Build the structured-error result envelope for this op.

    Purpose: uniform fail-loud shape, mirroring `commit_exec_bit.py :: _error`
    — mutation flags false, "error" naming what went wrong, plus any extra
    naming payload.
    """
    result: dict = {
        "committed": False,
        "sha": None,
        "error": message,
    }
    result.update(extra)
    return result


def _string_list(params: dict, key: str) -> Optional[List[str]]:
    """Validate an optional params field as a list of non-empty strings.

    Returns the coerced list on success, `None` when the field is absent,
    or raises `ValueError` naming the field on a malformed value — the
    handler turns that into a structured `_error(...)` result rather than
    letting a TypeError escape to the caller.
    """
    raw = params.get(key)
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        raise ValueError(f"params.{key} must be a list of strings")
    return raw


@register_op("ceremony.commit")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "ceremony.commit" handler — mutating, sync.

    Sync (not async): `run_commit_pipeline` performs synchronous subprocess
    I/O; ipc.py offloads sync handlers via asyncio.to_thread (the same
    pattern `commit_exec_bit.py` uses).

    Params:
        repo_root (str, optional) — D3 consistency assertion only
            (check_repo_root); NEVER the worktree-resolution source.
        subject (str, required) — the commit subject line, passed straight
            through to `run_commit_pipeline`'s own `subject` kwarg.
        prose (str, optional, default "") — commit body prose.
        stage_paths (list[str], required, non-empty) — the pathspec this
            commit stages and commits. An empty list is refused here rather
            than forwarded — `git commit -- <paths>` with an empty path list
            commits the WHOLE INDEX, not nothing (Anti-scope, this plan).
        deleted_paths (list[str], optional, default []).
        kept_entries (list[str], optional, default []).
        trailers (str, optional, default "").
        caller_paths (list[str], optional) — when omitted, derived as
            `set(stage_paths)`, mirroring every existing in-process caller
            (`safe_commit_offer.py`, `close_out_and_stamp.py`).
        push_mode (str, optional) — passed straight through; `None`/absent
            leaves `run_commit_pipeline`'s own default (`PUSH_MODE_SYNC`)
            unchanged.
        allow_protected_branch (bool, optional, default False).
        protected_branch_override_reason (str, optional).
        deliverable_id (str, optional).
        attributed_session_id (str, optional) — the caller's own committing
            session identity; the pipeline's own `session_id` kwarg is a
            confirmed dead nonce (see `run_commit_pipeline`'s docstring), so
            this handler mints a synthetic per-call nonce for it, same as
            every other in-process caller.

    Returns:
        {"committed": bool, "sha": str|None} on success, plus
        {"error": str} on any structured failure (mutation flags false).

    Keying scope: common_dir — repo_root arg is the .git directory; the
    caller's worktree is main_worktree_root(repo_root).
    """
    if repo_root is None:
        return _error(
            "ceremony.commit requires a common_dir-keyed dispatch; "
            "repo_root (git common dir) was not supplied"
        )

    subject = params.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        return _error("params.subject is required and must be a non-empty string")

    try:
        stage_paths = _string_list(params, "stage_paths")
        deleted_paths = _string_list(params, "deleted_paths") or []
        kept_entries = _string_list(params, "kept_entries") or []
        caller_paths_list = _string_list(params, "caller_paths")
    except ValueError as exc:
        return _error(str(exc))

    if not stage_paths:
        return _error(
            "params.stage_paths is required and must be a non-empty list of "
            "strings — an empty pathspec would commit the whole index"
        )

    d3_mismatch = check_repo_root(params.get("repo_root"), repo_root)
    if d3_mismatch is not None:
        return _error(d3_mismatch)

    worktree_root = main_worktree_root(repo_root)

    prose = params.get("prose", "")
    if not isinstance(prose, str):
        return _error("params.prose must be a string")

    trailers = params.get("trailers", "")
    if not isinstance(trailers, str):
        return _error("params.trailers must be a string")

    caller_paths = (
        set(caller_paths_list) if caller_paths_list is not None else set(stage_paths)
    )

    push_mode_kwargs: dict = {}
    if "push_mode" in params:
        push_mode = params.get("push_mode")
        if not isinstance(push_mode, str):
            return _error("params.push_mode must be a string")
        push_mode_kwargs["push_mode"] = push_mode

    allow_protected_branch = bool(params.get("allow_protected_branch", False))

    session_nonce = f"ceremony-commit-{uuid.uuid4().hex}"

    pipeline_result = run_commit_pipeline(
        worktree_root,
        session_id=session_nonce,
        subject=subject,
        prose=prose,
        deleted_paths=deleted_paths,
        kept_entries=kept_entries,
        trailers=trailers,
        stage_paths=stage_paths,
        caller_paths=caller_paths,
        allow_protected_branch=allow_protected_branch,
        protected_branch_override_reason=params.get("protected_branch_override_reason"),
        deliverable_id=params.get("deliverable_id"),
        attributed_session_id=params.get("attributed_session_id"),
        **push_mode_kwargs,
    )

    if pipeline_result.commit_failed:
        error = (
            "; ".join(pipeline_result.diagnostics)
            or "commit pipeline reported commit_failed with no diagnostics"
        )
        return _error(
            error,
            reason=pipeline_result.reason or None,
        )

    return {
        "committed": pipeline_result.committed_sha is not None,
        "sha": pipeline_result.committed_sha,
        "sha_unverified": pipeline_result.sha_unverified,
        "push_status": pipeline_result.push_status,
        "reason": pipeline_result.reason or None,
    }
