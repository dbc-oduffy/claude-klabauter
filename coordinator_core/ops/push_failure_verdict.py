"""
coordinator_core.ops.push_failure_verdict — JSON-RPC "git.push_failure_verdict"
operation.

Purpose: classify a non-fast-forward push failure into one of five states so
Coordinator-claude's Stop-hook auto-push advisory (`coordinator/hooks/scripts/
runtime-tripwire-em-check.py`, THEIR repo, not touched here) renders a verdict
we computed instead of re-deriving one from a regex. The classification is
the value this op sells; the advisory is a pure renderer over it.

Origin memos (read for full context, not reproduced here):
    cross-repo/inbox/2026-08-06-coordinator-claude-em-autopush-advisory-worktree-contention.md
    cross-repo/inbox/2026-08-06-coordinator-claude-em-autopush-advisory-yes-build-the-verdict-op.md

Op-key / contract:
    git.push_failure_verdict
    params:   {} (no required params — see handler docstring for repo_root
               resolution)
    response: {
        verdict: "peer_staged" | "half_applied_merge" | "simple_lag"
                 | "resolved_since" | "indeterminate",
        evidence: {
            staged_count: int,
            staged_sample: list[str]           (first 20, sorted),
            unstaged_local_count: int,
            incoming_count: int | None,         (None when upstream unresolvable)
            staged_incoming_overlap: int | None,
            staged_unstaged_overlap: int | None,
            ahead: int | None,
            behind: int | None,
            merge_head_present: bool,
            upstream_resolved: bool,
            push_failures_log_count: int,
            push_failures_log_newest: str | None,   (raw bracketed timestamp)
        },
        remedy_hint: str,
    }

Scope-verdict `show_top`: every signal this op reads — staged/unstaged diff,
MERGE_HEAD, upstream ahead/behind — is per-WORKTREE state (linked worktrees
each carry their own index, private gitdir, and upstream tracking config),
never the shared git COMMON dir; matches `coverage.gate`'s and `merge.
quiet_activity_gate`'s own `show_top` rationale in `op_scopes.py`. The one
exception is `push-failures.log` itself, which the writer (`hooks/auto_push.
py`) keys off the git COMMON dir via `resolve_git_common_dir` — this op
resolves that path the same way, independent of the `show_top` scope key
that governs `repo_root` injection.

Classification order (five states, checked top-down; see each branch's own
comment for the discriminator):
    1. Upstream unresolvable (no tracking branch, detached HEAD, not a repo)
       -> indeterminate.
    2. Staged set non-empty:
       - Incoming diff unavailable -> indeterminate (no discriminator data
         to confirm either staged state; guessing here is exactly the
         "always picks something" erosion the origin memo warns against).
       - High overlap(staged, incoming) AND ~zero overlap(staged, unstaged)
         -> half_applied_merge.
       - Otherwise -> peer_staged.
    3. Staged set empty:
       - push-failures.log has entries AND tree is fully in sync (ahead==0,
         behind==0, no MERGE_HEAD) -> resolved_since.
       - ahead>0 or behind>0 -> simple_lag.
       - Otherwise (fully clean, no log entries) -> indeterminate (nothing
         pathological to classify — the caller invoked this with no signal).

Negative-spec:
    - Read-only. Never stages, merges, resets, or pushes anything.
    - Never raises on git-availability/state degradation (missing log,
      no upstream, detached HEAD, empty repo, non-repo path) — every
      degradation collapses to a `GitResult`-shaped failure at the specific
      subprocess call, which this module reads as "signal absent" and folds
      into `indeterminate`, never a traceback.
    - Does NOT add a "best guess" tiebreak to drain `indeterminate` — see
      classification order step 2's "incoming diff unavailable" branch,
      which is deliberately NOT collapsed into `peer_staged` despite that
      being the operationally "safer" guess; the origin memo names this
      explicitly as a state that must stay genuinely reachable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.git_native import GitResult, _git

#: Overlap threshold (staged ∩ incoming) / len(staged) at/above which staged
#: content reads as "mostly the incoming commit's own files" rather than an
#: unrelated peer's WIP — the origin memo's live incident measured 58/63
#: (~0.92); set comfortably below that so a smaller but still-dominant
#: overlap still classifies, while a genuinely unrelated peer batch (typically
#: near-zero overlap with the remote diff) does not.
_HALF_APPLIED_OVERLAP_RATIO = 0.5

#: `staged_unstaged_overlap` at/below which "our own failed merge" reads as
#: confirmed rather than merely plausible — the origin memo's incident was
#: exactly zero; kept as a hard `== 0` rather than a ratio since a nonzero
#: overlap with the caller's OWN unstaged edits is itself evidence some of
#: the staged content is genuinely this session's WIP, not the merge's.
_HALF_APPLIED_MAX_UNSTAGED_OVERLAP = 0

#: Matches the auto_push.py writer's bracketed-UTC-stamp line prefix,
#: mirroring `workday_surface_auto_push_failure_stats._TS_RE` (kept as a
#: private duplicate rather than a cross-module import — this op only needs
#: the newest raw line, not that module's 24h-window aggregation).
_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\]")

_STAGED_SAMPLE_LIMIT = 20


def _split_nonempty_lines(stdout: str) -> List[str]:
    return [line for line in stdout.splitlines() if line.strip()]


def _staged_files(repo_root: Path) -> Optional[List[str]]:
    """`git diff --cached --name-only` — staged paths, or None on a git
    failure (never raises; folded into `indeterminate` by the caller).
    """
    result = _git(["diff", "--cached", "--name-only"], cwd=repo_root)
    if not result.ok:
        return None
    return _split_nonempty_lines(result.stdout)


def _unstaged_local_files(repo_root: Path) -> Optional[List[str]]:
    """`git diff --name-only` — unstaged working-tree modifications to
    TRACKED files (untracked files are deliberately excluded — this
    discriminator is about the caller's own in-progress edits to files git
    already knows, not the whole untracked surface)."""
    result = _git(["diff", "--name-only"], cwd=repo_root)
    if not result.ok:
        return None
    return _split_nonempty_lines(result.stdout)


def _merge_head_present(repo_root: Path) -> bool:
    """Whether MERGE_HEAD exists in THIS worktree's private gitdir.

    Resolved via `git rev-parse --git-dir` (not `resolve_git_common_dir`) —
    MERGE_HEAD is worktree-local state, present only in the private gitdir
    of the worktree where the merge was attempted, matching this op's
    `show_top` scope-verdict for every other signal it reads.
    """
    git_dir_result = _git(["rev-parse", "--git-dir"], cwd=repo_root)
    if not git_dir_result.ok:
        return False
    git_dir_raw = git_dir_result.stdout.strip()
    if not git_dir_raw:
        return False
    git_dir = Path(git_dir_raw)
    if not git_dir.is_absolute():
        git_dir = repo_root / git_dir
    return (git_dir / "MERGE_HEAD").exists()


def _upstream_ahead_behind(repo_root: Path) -> Optional[tuple]:
    """`git rev-list --left-right --count @{u}...HEAD` -> (ahead, behind), or
    None when no upstream is configured / detached HEAD / not a repo. Output
    shape is `"<behind>\\t<ahead>"` (left = @{u}, right = HEAD)."""
    result = _git(
        ["rev-list", "--left-right", "--count", "@{u}...HEAD"], cwd=repo_root
    )
    if not result.ok:
        return None
    parts = result.stdout.strip().split()
    if len(parts) != 2:
        return None
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return ahead, behind


def _incoming_files(repo_root: Path) -> Optional[List[str]]:
    """Files the incoming (upstream) commits touch relative to the current
    merge-base — `git diff --name-only HEAD...@{u}` (triple-dot: diff
    against the merge-base of HEAD and upstream, i.e. exactly what a
    successful merge/rebase would bring in). None on any git failure
    (no upstream, detached HEAD, not a repo)."""
    result = _git(["diff", "--name-only", "HEAD...@{u}"], cwd=repo_root)
    if not result.ok:
        return None
    return _split_nonempty_lines(result.stdout)


def _push_failures_log_evidence(repo_root: Path) -> tuple:
    """Return (count, newest_raw_timestamp_or_None) over `push-failures.log`,
    keyed at the git COMMON dir (writer's own keying — see module docstring).
    Absent log -> (0, None), never an error (mirrors `workday_surface_
    auto_push_failure_stats`'s own absent-log contract)."""
    log_path = resolve_git_common_dir(repo_root) / "push-failures.log"
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, NotADirectoryError):
        return 0, None
    lines = [line for line in text.splitlines() if line.strip()]
    newest: Optional[str] = None
    for line in reversed(lines):
        match = _TS_RE.match(line)
        if match is not None:
            newest = match.group(1)
            break
    return len(lines), newest


def classify(repo_root: Path) -> dict:
    """Compute the five-state verdict + evidence for `repo_root` (a resolved
    worktree root). Pure read; see module docstring for the classification
    order and each state's discriminator."""
    staged = _staged_files(repo_root)
    unstaged = _unstaged_local_files(repo_root)
    merge_head_present = _merge_head_present(repo_root)
    ahead_behind = _upstream_ahead_behind(repo_root)
    log_count, log_newest = _push_failures_log_evidence(repo_root)

    upstream_resolved = ahead_behind is not None
    ahead: Optional[int]
    behind: Optional[int]
    if ahead_behind is not None:
        ahead, behind = ahead_behind
    else:
        ahead, behind = None, None

    incoming: Optional[List[str]] = None
    staged_incoming_overlap: Optional[int] = None
    staged_unstaged_overlap: Optional[int] = None
    if staged is not None and upstream_resolved:
        incoming = _incoming_files(repo_root)
        if incoming is not None:
            staged_set = set(staged)
            unstaged_set = set(unstaged) if unstaged is not None else set()
            staged_incoming_overlap = len(staged_set & set(incoming))
            staged_unstaged_overlap = len(staged_set & unstaged_set)

    evidence = {
        "staged_count": len(staged) if staged is not None else 0,
        "staged_sample": sorted(staged)[:_STAGED_SAMPLE_LIMIT] if staged else [],
        "unstaged_local_count": len(unstaged) if unstaged is not None else 0,
        "incoming_count": len(incoming) if incoming is not None else None,
        "staged_incoming_overlap": staged_incoming_overlap,
        "staged_unstaged_overlap": staged_unstaged_overlap,
        "ahead": ahead,
        "behind": behind,
        "merge_head_present": merge_head_present,
        "upstream_resolved": upstream_resolved,
        "push_failures_log_count": log_count,
        "push_failures_log_newest": log_newest,
    }

    # Step 1 -- upstream unresolvable: no tracking branch, detached HEAD, or
    # a git-invocation failure so basic no signal to reason about is safe.
    if not upstream_resolved:
        return {
            "verdict": "indeterminate",
            "evidence": evidence,
            "remedy_hint": (
                "no upstream tracking branch resolvable (detached HEAD, "
                "unconfigured upstream, or a git failure) -- cannot classify "
                "without ahead/behind data"
            ),
        }

    # Step 2 -- staged set non-empty.
    if staged:
        if incoming is None:
            return {
                "verdict": "indeterminate",
                "evidence": evidence,
                "remedy_hint": (
                    f"{len(staged)} file(s) staged but the incoming-commit "
                    "diff could not be computed -- insufficient data to tell "
                    "a peer's staged WIP from our own half-applied merge; "
                    "stand off rather than guess"
                ),
            }
        overlap_ratio = (
            staged_incoming_overlap / len(staged) if staged_incoming_overlap else 0.0
        )
        if (
            overlap_ratio >= _HALF_APPLIED_OVERLAP_RATIO
            and (staged_unstaged_overlap or 0) <= _HALF_APPLIED_MAX_UNSTAGED_OVERLAP
        ):
            return {
                "verdict": "half_applied_merge",
                "evidence": evidence,
                "remedy_hint": (
                    f"{staged_incoming_overlap} of {len(incoming)} incoming files "
                    f"staged, {staged_unstaged_overlap or 0} overlap with local "
                    "modifications -- this is our own failed merge's partial "
                    "index: git reset (mixed), scoped-commit the blockers, re-merge"
                ),
            }
        return {
            "verdict": "peer_staged",
            "evidence": evidence,
            "remedy_hint": (
                f"{len(staged)} file(s) staged do not read as the incoming "
                "commit's own content -- likely another session's "
                "work-in-progress; stand off, touch nothing"
            ),
        }

    # Step 3 -- staged set empty.
    if log_count > 0 and (ahead or 0) == 0 and (behind or 0) == 0 and not merge_head_present:
        return {
            "verdict": "resolved_since",
            "evidence": evidence,
            "remedy_hint": (
                f"{log_count} push-failures.log entr{'y' if log_count == 1 else 'ies'} "
                "on record, but the tree is now fully in sync -- the failure was "
                "real when written and has since been superseded (most likely a "
                "peer reconciled and pushed); nothing to push"
            ),
        }

    if (ahead or 0) > 0 or (behind or 0) > 0:
        return {
            "verdict": "simple_lag",
            "evidence": evidence,
            "remedy_hint": (
                f"clean index, {ahead} ahead / {behind} behind upstream -- "
                "git push (or pull-then-push) is genuinely the remedy"
            ),
        }

    return {
        "verdict": "indeterminate",
        "evidence": evidence,
        "remedy_hint": (
            "clean index, fully in sync with upstream, no push-failures.log "
            "entries -- no pathological signal to classify"
        ),
    }


@register_op("git.push_failure_verdict")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'git.push_failure_verdict' handler.

    `repo_root` is the dispatch-supplied CALLING worktree (scope-verdict
    `show_top`) — this op takes no params of its own (frozen contract has
    none); falls back to `Path.cwd()` only for a standalone invocation with
    no injected `repo_root` (mirrors `merge.quiet_activity_gate`'s own
    handler convention).
    """
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    return classify(root)
