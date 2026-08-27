"""
coordinator_core.ops.ceremony.commit_v2 -- JSON-RPC "ceremony.commit_v2" op.

Purpose: a fresh dispatchable identity over the 694 measured, zero-spawn lines
in `coordinator_core/git/commit.py` (`commit_paths`) and
`coordinator_core/git/index_write.py` (`splice_index`, called internally by
`commit_paths`). `ceremony.commit` is DEAD (killed at p50 421.9ms process
time against a 200ms bar -- `coordinator_core/op_budget_suspension.py`) and
ITS NAME STAYS DEAD: this op is a fresh identity, ONCE, so every guard, budget
row and test fixture keyed to the old name is unambiguous about which op it
describes. Do not resurrect "ceremony.commit" for this handler or any other.

Handler shape: a thin envelope, not a wrapper. It calls `commit.commit_paths`
directly and returns its outcome. It does NOT import `run_commit_pipeline`,
and does NOT import anything from `commit_pipeline.py` or `git_native.py` --
those two modules are the 11,015-line surface this op exists to make
un-necessary, and importing from them here would make the v2 a second path
into the same pipeline rather than a replacement for it
(docs/plans/2026-08-27-something-must-commit-ceremony-commit-v2.md, C3 body).

Keying scope: common_dir (`coordinator_core/op_scopes.py`) -- commits within
the CALLER's own working tree/index, mirroring `commit.exec_bit_change` and
`commit.anchors`'s precedent: the handler receives repo_root = git common dir
and derives the caller's worktree via `main_worktree_root(repo_root)`.
`params.repo_root` is the optional D3 consistency assertion only
(`check_repo_root`), never the worktree-resolution source.

Scope of THIS row (C3): register the op and wire it straight to
`commit_paths`. It does NOT yet fix the two known correctness gaps
(exec-bit-on-commit, the `eol=crlf` CR-byte fallback) -- those are C4's row,
landing in `commit.py` itself, which this handler picks up for free once C4
lands (no `blob_fallback` is supplied here, so a `FilterUnsupported` refusal
propagates as a structured error rather than being silently swallowed).

Spec backlinks:
    docs/plans/2026-08-27-something-must-commit-ceremony-commit-v2.md § C3
    coordinator_core/git/commit.py :: commit_paths
    coordinator_core/git/index_write.py :: splice_index (called internally)

Negative-spec (hard-won, restated for this row):
  - Does NOT import `coordinator_core.ops.ceremony.commit_pipeline` or
    `coordinator_core.ops.ceremony.git_native` in any form -- not
    `run_commit_pipeline`, not a helper, not a type. If the handler needs
    something those modules have, it is restated in `git/`, small, or it does
    not come (C3 body, verbatim).
  - Does NOT use the name "ceremony.commit" anywhere -- registry key,
    docstring, error message, or test fixture. That identity is dead and
    stays dead.
  - Does NOT catch `CommitRefused`/`FilterUnsupported` and retry, guess, or
    widen scope -- a refusal from `commit_paths` is returned as a structured
    error, unmodified in substance, so the caller sees exactly why nothing
    was written.
  - Does NOT use `params.repo_root` as the worktree-resolution source (D3:
    socket-authoritative common_dir only).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.git.commit import CommitRefused, FilterUnsupported, commit_paths
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import check_repo_root, main_worktree_root


def _error(message: str, **extra: object) -> dict:
    """Build the structured-error result envelope for this op.

    Purpose: uniform fail-loud shape -- contract fields present with
    "committed" false and "sha" null, plus "error" naming what happened.
    """
    result: dict = {"committed": False, "sha": None, "error": message}
    result.update(extra)
    return result


@register_op("ceremony.commit_v2")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "ceremony.commit_v2" handler -- mutating, sync.

    Sync (not async): `commit_paths` does synchronous filesystem I/O only
    (zero git spawns on the common shapes); ipc.py offloads sync handlers via
    asyncio.to_thread (commit_exec_bit / commit_anchors pattern).

    Params:
        repo_root (str, optional)   -- D3 consistency assertion only
                                       (check_repo_root); NEVER the
                                       worktree-resolution source.
        paths     (list[str], required) -- repo-relative paths to commit.
                                       At least one of `paths`/`deleted_paths`
                                       must be non-empty (an empty pathspec is
                                       refused by `commit_paths` itself --
                                       it commits the WHOLE INDEX otherwise).
        deleted_paths (list[str], optional) -- repo-relative paths to record
                                       as removed in this commit.
        message   (str, required)   -- the commit message.
        prefer_staged (list[str], optional) -- paths whose STAGED bytes are
                                       committed in preference to differing
                                       worktree bytes (commit_paths invariant
                                       1 -- a deliberate partial stage,
                                       declared, never inferred).

    Returns:
        {"committed": True, "sha": str, "staged_preferred": [str, ...]}
        on success, or {"committed": False, "sha": None, "error": str} on any
        structured refusal (an empty pathspec, a directory in `paths`, an
        unresolvable CAS ref, a lost CAS race, or a path needing a checkin
        conversion this module does not yet reproduce -- the `eol=crlf`
        fallback lands in C4, not here).

    Keying scope: common_dir -- repo_root arg is the .git common dir; the
    caller's worktree is main_worktree_root(repo_root).
    """
    if repo_root is None:
        return _error(
            "ceremony.commit_v2 requires a common_dir-keyed dispatch; "
            "repo_root (git common dir) was not supplied"
        )

    d3_mismatch = check_repo_root(params.get("repo_root"), repo_root)
    if d3_mismatch is not None:
        return _error(d3_mismatch)

    raw_paths = params.get("paths") or []
    if not isinstance(raw_paths, list) or not all(isinstance(p, str) for p in raw_paths):
        return _error("params.paths must be a list of strings")

    raw_deleted = params.get("deleted_paths") or []
    if not isinstance(raw_deleted, list) or not all(isinstance(p, str) for p in raw_deleted):
        return _error("params.deleted_paths must be a list of strings")

    message = params.get("message")
    if not isinstance(message, str) or not message.strip():
        return _error("params.message is required and must be a non-empty string")

    raw_prefer_staged = params.get("prefer_staged") or []
    if not isinstance(raw_prefer_staged, list) or not all(
        isinstance(p, str) for p in raw_prefer_staged
    ):
        return _error("params.prefer_staged must be a list of strings")

    worktree_root = main_worktree_root(repo_root)

    try:
        outcome = commit_paths(
            worktree_root,
            raw_paths,
            message,
            deleted_paths=raw_deleted,
            prefer_staged=raw_prefer_staged,
        )
    except (CommitRefused, FilterUnsupported) as exc:
        return _error(str(exc))

    return {
        "committed": True,
        "sha": outcome.sha,
        "staged_preferred": list(outcome.staged_preferred),
    }
