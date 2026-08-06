"""
coordinator_core.ops.ceremony.detached_render_commit — shared self-commit helper
for detached-render CLIs (C5 residue, 2026-07-23 wsc-tail-slim-down).

Purpose: `render-handoff-tracker.py` and `refresh-roadmap-callout.py` now fire
DETACHED (`tail_ops.fire_tracker_and_roadmap_detached`), post-ceremony-commit,
alongside C2's four detached archive-sweep children -- all racing to commit into
the SAME repo. `extra_stage_paths` used to fold each render's output into the
SAME commit as the ceremony's own blocking commit; a detached render has no such
ride-along commit to fold into, so each render must commit its OWN artifact with
an explicit pathspec or leave an uncommitted dirty file that the NEXT ceremony's
`dirty_tree_gate` (whole-worktree scan) hard-fails on as unattributable.

Concurrent `git commit` invocations contend on `.git/index.lock`, which git does
NOT retry on its own -- `commit_own_artifact()` is the ONE shared "stage + commit,
bounded retry on lock contention, log on exhaustion" call both renders use, so the
retry/backoff policy and failure-log wiring are get-right-once rather than
hand-copied per call site and independently drift-prone (mirrors
`detached_spawn.py`'s own rationale for the spawn half of this same C17 family).

Contract: `commit_own_artifact(repo_root, rel_path, message, caller_label=...)`
NEVER raises. Returns True when a commit landed OR there was genuinely nothing to
commit (both are success) -- False only after the bounded retry budget is
exhausted on a real `.git/index.lock` contention, or immediately on any OTHER git
failure (no retry burned on a non-lock error). On a False return, a `CHILD FAILED`
record has already been appended to the shared housekeeping-failures log via
`coordinator_core.ops.ceremony.detached_spawn.record_child_failure`, so C17b's
orientation-cache surfacing pass sees it -- callers must NOT swallow the False
return silently, but also must NOT block or spin indefinitely waiting on it (the
whole point of the bounded retry).

Negative-spec:
  - Never `git add -A` / `git add .` / bare `git commit` -- always a single,
    explicit relative pathspec (`-- <rel_path>`), scoped to exactly the one
    artifact the caller owns. A concurrent peer's own staged-but-uncommitted
    changes elsewhere in the tree are never touched.
  - Does NOT decide WHETHER a commit is warranted (e.g. the Rule-5 meta-repo vs.
    sibling-repo branch, or "did the render actually run") -- that classification
    is the CALLER's job (see `render_handoff_tracker.main` /
    `refresh_roadmap_callout.main`). This module is pure commit-mechanics: given a
    path the caller has already decided is its own artifact, stage-and-commit it
    if (and only if) it is actually dirty.
  - Does NOT retry a non-lock git failure (e.g. a genuine `git commit` config/
    permissions error) -- that is a terminal failure, recorded once immediately.
  - Does NOT hold any process-level lock of its own (no `ceremony_lock`) -- a
    detached render is explicitly OUTSIDE the ceremony's own lock/commit critical
    section; contention is expected and absorbed here via the git-level retry,
    not via a higher-level mutex.

Spec backlink: docs/plans/2026-07-23-wsc-tail-slim-down.md § C5 (Artifact
disposition residue)
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Union

from coordinator_core.ops.ceremony.detached_spawn import record_child_failure

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: Bounded retry policy for `.git/index.lock` contention: 1 initial attempt + 3
#: retries (4 total), fixed backoff schedule (seconds, slept BEFORE attempts
#: 2/3/4) tuned for the sub-second lock-hold window a single-path `git add` +
#: `git commit` normally clears in -- C2's sibling archive-sweep children are
#: themselves quick, single-path commits, not long-held locks. Never spins
#: indefinitely: the schedule is fixed-length, not open-ended.
_MAX_ATTEMPTS = 4
_BACKOFF_SCHEDULE_S = (0.1, 0.3, 0.7)

#: Substrings that identify a `.git/index.lock` contention failure in git's own
#: stderr -- the ONLY class of failure this helper retries. Any other stderr
#: (permissions, hook rejection, bad config, etc.) is terminal on first sight.
_LOCK_CONTENTION_SIGNATURES = (
    "index.lock",
    "Another git process seems to be running",
)


def _is_lock_contention(stderr: str) -> bool:
    return any(sig in stderr for sig in _LOCK_CONTENTION_SIGNATURES)


def _run_git(args, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
    )


def commit_own_artifact(
    repo_root: Union[str, Path],
    rel_path: str,
    message: str,
    *,
    caller_label: str,
) -> bool:
    """Stage and commit exactly one path (explicit pathspec), retrying only on
    `.git/index.lock` contention.

    Args:
        repo_root: the git worktree root to run `git` from (cwd).
        rel_path: the SINGLE path this call may stage/commit, relative to
            `repo_root` (or absolute -- git accepts either as a pathspec run
            with `cwd=repo_root`). Never widened to a directory or glob.
        message: the commit subject.
        caller_label: a short, human-readable identifier for the *caller*
            (e.g. ``"render-handoff-tracker.py:self-commit"``), recorded in the
            shared housekeeping-failures log on a terminal failure so a reader
            can tell which detached render's follow-up commit failed.

    Returns:
        True  -- a commit landed, OR `rel_path` was already clean (nothing to
                 commit is a SUCCESS, not a failure -- no commit, no log entry).
        False -- every retry was exhausted against persistent lock contention,
                 or a non-lock git failure occurred. A `CHILD FAILED` record has
                 already been appended to the shared housekeeping-failures log.

    Never raises.
    """
    root = Path(repo_root)
    last_stderr = ""

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        if attempt > 1:
            time.sleep(_BACKOFF_SCHEDULE_S[attempt - 2])

        try:
            add_result = _run_git(["add", "--", rel_path], root)
        except OSError as exc:
            record_child_failure(str(root), caller_label, exc=exc)
            return False

        if add_result.returncode != 0:
            last_stderr = add_result.stderr.strip()
            if _is_lock_contention(last_stderr):
                continue
            record_child_failure(
                str(root), caller_label,
                exc=RuntimeError(f"git add -- {rel_path} failed: {last_stderr}"),
            )
            return False

        # Nothing staged relative to HEAD -> genuinely no change to commit.
        # `git diff --cached --quiet` exit-code contract: 0 == no diff (clean),
        # 1 == diff exists -- this is the success/no-op case, not a failure.
        try:
            diff_result = _run_git(["diff", "--cached", "--quiet", "--", rel_path], root)
        except OSError as exc:
            record_child_failure(str(root), caller_label, exc=exc)
            return False

        if diff_result.returncode == 0:
            return True

        try:
            commit_result = _run_git(
                ["-c", "commit.gpgsign=false", "commit", "-m", message, "--", rel_path],
                root,
            )
        except OSError as exc:
            record_child_failure(str(root), caller_label, exc=exc)
            return False

        if commit_result.returncode == 0:
            return True

        last_stderr = commit_result.stderr.strip()
        if _is_lock_contention(last_stderr):
            continue
        record_child_failure(
            str(root), caller_label,
            exc=RuntimeError(f"git commit -- {rel_path} failed: {last_stderr}"),
        )
        return False

    record_child_failure(
        str(root), caller_label,
        exc=RuntimeError(
            f"git commit -- {rel_path}: index.lock contention persisted after "
            f"{_MAX_ATTEMPTS} attempts: {last_stderr}"
        ),
    )
    return False
