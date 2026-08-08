"""
coordinator_core.ops.merge_branch_into_workstream — idempotent branch
consolidation into the active workstream branch (op
`branch.merge_into_workstream`).

Purpose: native replacement for the
`coordinator/pipelines/workday-start-internals.md:203` fence
(`COORDINATOR_OVERRIDE_BRANCH=1 COORDINATOR_OVERRIDE_BRANCH_REASON=...
git merge {branch} --no-ff -m "consolidate {branch} into active workstream
branch"`). Two hazards closed per settlement A4
(docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md § A4,
RATIFIES):

  - Re-run correctness: `git merge` is inherently non-idempotent — a second
    invocation after success either no-ops or conflicts. Closed by a
    pre-check via `orphan_branch_sweep._is_ancestor` (reused, NOT
    reimplemented — `git merge-base --is-ancestor`): branch already an
    ancestor of HEAD → `{merged: false, already_ancestor: true}` no-op.
  - Windows portability: the fence's `VAR=1 VAR2=... git merge ...`
    inline-env-var prefix is POSIX-shell syntax, invalid in cmd/PowerShell.
    Closed by passing the override variables via `subprocess` `env={**
    os.environ, ...}` — no shell syntax anywhere in the chain (CC-1).

Conflict path (preserved from the fence's own documented handling): on a
merge conflict this op runs `git merge --abort` — the caller is NEVER left
holding a conflicted index — and returns `{merged: false, conflict: true}`.

Idempotency (DEC-7 / AC7-AC8 note): achieved by the ancestry pre-check. A
successful merge makes *branch* an ancestor of HEAD, so the rerun with
identical inputs short-circuits at the pre-check and returns the documented
no-op shape `{merged: false, already_ancestor: true, conflict: false}` —
the merge subprocess never runs a second time.

CC-7 fail-loud: a `git merge` failure with NO merge in progress (no
`MERGE_HEAD` — e.g. dirty worktree refusal, unrelated histories with no
conflict state) is an unclassified half-state, not a conflict; it raises a
structured error naming git's stderr rather than mislabeling it
`conflict: true`.

`_is_ancestor` reuse: `orphan_branch_sweep._is_ancestor` already carries a
`cwd=` keyword on disk (landed by the C1c quartet chunk), so this module
calls it directly with `cwd=repo` — no signature-adaptive shim. (Review:
code-reviewer — a prior `inspect.signature`/`os.chdir` compat adapter here
was dead code once the peer module landed `cwd=`, and `os.chdir` is
process-global, a latent hazard under `asyncio.to_thread`; removed.)

Negative-spec: this op never resolves conflicts, never force-merges, never
switches branches, and never deletes the source branch after merging —
consolidation cleanup is the caller's ceremony.

Op contract (C0a manifest row `merge-branch-into-active-workstream`):
    branch.merge_into_workstream
    params: {branch: str, reason: str}
    -> {merged: bool, already_ancestor: bool, conflict: bool}
    scope: show_top (mutates the checked-out worktree's HEAD/index;
           `coverage.gate` precedent)

Spec backlink: docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md § A4
               docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md
Fence source: coordinator/pipelines/workday-start-internals.md:203 (example-doctrine-repo)
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from coordinator_core.win_portability import no_console_creationflags
from pathlib import Path
from typing import Optional, Union

from coordinator_core.ipc import register_op
from coordinator_core.ops.orphan_branch_sweep import _is_ancestor

_PathLike = Union[str, Path, None]

_GIT_TIMEOUT = 60


def _git(
    args: list[str], cwd: _PathLike = None, env: Optional[dict] = None
) -> subprocess.CompletedProcess:
    """Direct list-argv git subprocess (CC-1) with hang cap + console suppression.

    Override variables travel via the `env=` dict — never a POSIX inline-env
    prefix (settlement A4's Windows-portability closure).
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=_GIT_TIMEOUT,
        **no_console_creationflags(),
    )


def _branch_is_ancestor(candidate_sha: str, of_sha: str, repo: Path) -> bool:
    """Call the reused `orphan_branch_sweep._is_ancestor` against *repo*.

    Direct `cwd=` call — no `os.chdir` fallback (see module docstring: the
    peer module's `cwd=` keyword is already on disk in this diff's combined
    state, and `os.chdir` is process-global, a hazard under
    `asyncio.to_thread`).
    """
    return _is_ancestor(candidate_sha, of_sha, cwd=repo)


def _rev_parse(rev: str, repo: Path) -> Optional[str]:
    res = _git(["rev-parse", "--verify", "--quiet", rev], cwd=repo)
    if res.returncode != 0:
        return None
    sha = res.stdout.strip()
    return sha or None


def _merge_head_path(repo: Path) -> Path:
    res = _git(["rev-parse", "--git-path", "MERGE_HEAD"], cwd=repo)
    raw = res.stdout.strip()
    p = Path(raw)
    return p if p.is_absolute() else repo / p


def merge_branch_into_workstream(
    branch: str, reason: str, repo_root: Union[str, Path]
) -> dict:
    """Idempotently merge *branch* into the checked-out workstream branch.

    Raises (CC-7 fail-loud):
      ValueError   — empty branch, repo_root not a git worktree, branch
                     unresolvable.
      RuntimeError — merge failed with no conflict in progress, or
                     `git merge --abort` itself failed (tree left dirty —
                     surfaced, never silently swallowed).
    """
    if not branch:
        raise ValueError("branch.merge_into_workstream: `branch` is required")
    repo = Path(repo_root)
    if _git(["rev-parse", "--git-dir"], cwd=repo).returncode != 0:
        raise ValueError(
            f"branch.merge_into_workstream: {repo} is not a git worktree"
        )

    branch_sha = _rev_parse(f"refs/heads/{branch}", repo)
    if branch_sha is None:
        raise ValueError(
            f"branch.merge_into_workstream: branch {branch!r} does not "
            f"resolve to a local ref in {repo}"
        )
    head_sha = _rev_parse("HEAD", repo)
    if head_sha is None:
        raise ValueError(
            f"branch.merge_into_workstream: HEAD does not resolve in {repo} "
            "(unborn branch?)"
        )

    # Idempotency pre-check (settlement A4): already merged → no-op.
    if _branch_is_ancestor(branch_sha, head_sha, repo):
        return {"merged": False, "already_ancestor": True, "conflict": False}

    env = {
        **os.environ,
        "COORDINATOR_OVERRIDE_BRANCH": "1",
        "COORDINATOR_OVERRIDE_BRANCH_REASON": reason or "",
    }
    res = _git(
        [
            "merge",
            branch,
            "--no-ff",
            "-m",
            f"consolidate {branch} into active workstream branch",
        ],
        cwd=repo,
        env=env,
    )
    if res.returncode == 0:
        return {"merged": True, "already_ancestor": False, "conflict": False}

    if _merge_head_path(repo).exists():
        # Conflict in progress — restore a clean tree before returning
        # (fence-documented abort path; caller never inherits a conflicted
        # index).
        abort = _git(["merge", "--abort"], cwd=repo, env=env)
        if abort.returncode != 0:
            raise RuntimeError(
                "branch.merge_into_workstream: git merge --abort failed after "
                f"a conflicted merge of {branch!r} — worktree left dirty: "
                f"{abort.stderr.strip()}"
            )
        return {"merged": False, "already_ancestor": False, "conflict": True}

    raise RuntimeError(
        f"branch.merge_into_workstream: git merge {branch!r} failed with no "
        f"merge in progress (not a conflict): {res.stderr.strip() or res.stdout.strip()}"
    )


@register_op("branch.merge_into_workstream")
async def _merge_branch_into_workstream_handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC `branch.merge_into_workstream` handler.

    params: {branch: str, reason: str}
    -> {merged: bool, already_ancestor: bool, conflict: bool}

    show_top scope: the target is the IPC-injected `repo_root` (the caller's
    checked-out worktree) — the merge mutates per-worktree HEAD/index state.
    """
    if repo_root is None:
        raise ValueError(
            "branch.merge_into_workstream: no IPC-injected repo_root "
            "(show_top-scoped op requires the caller worktree)"
        )
    branch = params.get("branch") or ""
    reason = params.get("reason") or ""
    return await asyncio.to_thread(
        merge_branch_into_workstream, branch, reason, repo_root
    )
