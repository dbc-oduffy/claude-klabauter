"""coordinator_core.lock_preflight — the shared orphaned-`.git/index.lock`
self-heal pre-flight every commit seam calls before its own git call lands.

Purpose: `coordinator/bin/scoped-git-commit` carried this exact pre-flight
(`_preflight_reap_stale_lock`) for CLI callers, and C2 of
`docs/plans/2026-08-13-commit-seams-inherit-lock-reap-and-retry.md` added a
VERBATIM second copy at `coordinator_core/ops/ceremony/commit_pipeline.py`
for in-process op callers that never reach the CLI at all. Two verbatim
copies of one policy is the exact drift-prone shape
`coordinator_core/git_lock_retry.py` (read its docstring — this module's own
design template) was created to end: a leaf module every commit seam
consumes, so the policy is get-right-once rather than hand-copied and
independently drift-prone. This module is that lift, applied to the
pre-flight reap rather than the retry loop `git_lock_retry` owns.

state/bug-backlog/2026-08-12-scoped-git-commit-is-not-a-raw-git-invoc-
f4fff3a626fa.yaml (P1): `guard_reap_stale_git_lock`'s PreToolUse guard only
recognizes a bare `git` in command position (`_seg_resolved_git_subcommand`)
— neither the CLI wrapper nor an in-process op caller ever matches it, so
each has to self-heal on its own rather than relying on the guard.

Cheap by construction: the overwhelming common case is "no lock at all", and
that case costs exactly one `os.path.exists`/`os.path.isdir` stat, no
subprocess. `ops/reap_stale_locks.py` (its own `git rev-parse` resolution,
age/stability re-sample, and exit-code ladder) is only invoked once a lock
file is actually present at the cheap-checked candidate path — resolved via
an in-process call to `reap_stale_locks.main([])`, deliberately NOT a
subprocess spawn, mirroring both copies this module replaces.

Negative-spec:
    - Never treats a reaper failure as a commit failure. ANY exception —
      import failure, subprocess failure, permission error, whatever — is
      swallowed here; the caller proceeds exactly as if this function had
      never been called. Fail-open is the whole point: a reaper defect must
      never block a commit that would otherwise succeed.
    - Never second-guesses or bypasses the reaper's own age/stability gate.
      `reap_stale_locks.main()`'s exit code 2 (a FRESH `index.lock` — a live
      commit may genuinely be in progress) is NOT reaped, and is treated
      identically to exit 0 here: the caller proceeds to its normal git
      call(s) either way, and git itself reports if it is genuinely
      blocked. That gate is the only thing standing between this and
      corrupting a peer's in-flight commit on a shared tree.
    - Never reimplements `do_reap`/`stale_and_stable`'s lock-selection or
      removal logic here — the whole-repo sweep in
      `reap_stale_locks.main()` is called as-is, so `index.lock`,
      `next-index-*.lock`, and `objects/maintenance.lock` share one reaper
      implementation regardless of caller.
    - Never assumes `<worktree_root>/.git` is a directory. In a linked `git
      worktree` or a submodule it is a FILE (a `gitdir: <path>` pointer to
      the real git dir elsewhere), so `<worktree_root>/.git/index.lock` can
      never exist there and a directory-only gate would silently never
      fire — the same narrowing class `guard_reap_stale_git_lock.py` was
      independently fixed for. This function does NOT parse that pointer
      file itself (that is git-dir-discovery reimplementation, reserved to
      the reaper); it pays one `os.path.isdir` call to tell directory from
      file, and
      for the file case delegates entirely to `reap_stale_locks.main()`,
      which already resolves the real git dir correctly via
      `--absolute-git-dir` / `--git-common-dir`.
    - BOTH the CLI (`coordinator/bin/scoped-git-commit`) and the op pipeline
      (`coordinator_core/ops/ceremony/commit_pipeline.py::
      run_commit_pipeline`) call this function — deliberately, not
      duplicatively: the CLI serves callers that never reach the op-level
      pipeline at all (a direct subprocess invocation of the CLI outside
      any op route), and the reaper itself is idempotent, so a lock already
      reaped by one caller costs the other nothing but the cheap
      `os.path.exists` check finding no lock. Do NOT "optimise away" either
      call site — a later reader collapsing them to one would silently
      un-cover the caller population the other one exists for.
    - Imports stdlib only. No import from `coordinator_core.ops.*` (except
      the deferred, function-local import of `reap_stale_locks` itself,
      needed to reach the reaper without creating a layering cycle at
      MODULE import time), `coordinator_core.contract.*`, or any assembler
      package — a leaf module every commit seam (including future ones)
      can depend on without a layering cycle.

Contract pinned by `coordinator/bin/tests/test_scoped_git_commit_lock_
reap.py` (CLI copy, pre-existing) and `coordinator_core/tests/
test_lock_preflight.py` (this leaf, mirrored fixtures): (1) a stale, stable
lock is reaped; (2) a fresh lock is left alone (exit 2 is "do not reap", not
a failure); (3) a reaper exception never propagates; (4) no lock present
costs no subprocess at all.
"""
from __future__ import annotations

import os


def preflight_reap_stale_lock(worktree_root: str) -> None:
    """Best-effort orphaned-`.git/index.lock` self-heal, run once before a
    commit seam's own git call(s) land on the pre-flight path. See this
    module's docstring for the full contract and negative-spec.
    """
    try:
        git_entry = os.path.join(worktree_root, ".git")
        try:
            is_dir = os.path.isdir(git_entry)
        except OSError:
            return
        if is_dir:
            lock_path = os.path.join(git_entry, "index.lock")
            if not os.path.exists(lock_path):
                return
        elif not os.path.exists(git_entry):
            return
        # else: `.git` is a file (linked worktree / submodule) -- the real
        # git dir cannot be resolved cheaply, so the sweep is called
        # unconditionally to let it resolve it correctly.
        from coordinator_core.ops import reap_stale_locks

        reap_stale_locks.main([])
    except Exception:
        return
