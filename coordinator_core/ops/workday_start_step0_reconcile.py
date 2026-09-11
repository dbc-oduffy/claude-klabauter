"""
coordinator_core.ops.workday_start_step0_reconcile — Step 0.4.5 (Reconcile
with origin/main) for the `/workday-start` precedence-switch flow.

Purpose: DR-059 bash-to-naked-Python port of the DoE-owned reconcile script.
Called by the /workday-start Step 0 flow after the branch-precedence switch
resolves on a non-main active branch. Folds `origin/main` into the current
branch so the active workstream stays mergeable; never abandons in-progress
work.

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292
Port of: workday-start-step0-reconcile.sh (DoE b5a4192c, 2026-07-20)

Negative-spec: does NOT check out `main`, does NOT push, does NOT resolve
merge conflicts itself — a conflict is left staged mid-merge-abort-cycle
(aborted) and surfaced to the caller as exit code 3 for the PM's A/B/C
Branch Reconciliation Decision.

Exit codes (parity-critical, preserved from the bash oracle):
    0 — already-includes (`ALREADY-CURRENT`) / fast-forward
        (`RECONCILED-FF`) / non-ff merge succeeded (`RECONCILED-MERGE`).
    3 — a failed `--no-ff` merge, discriminated (MERGE_HEAD-first, spawn-
        free) into one of three outcome strings: `RECONCILE-CONFLICT`
        (genuine content conflict; merge aborted, PM resolves first),
        `RECONCILE-MERGE-COMMIT-REFUSED` (the merge applied and reached the
        commit step but was refused there, e.g. by a commit hook), or
        `RECONCILE-MERGE-NOT-STARTED` (the merge never began — dirty
        worktree, unrelated histories, bad ref). None of the three is the
        A/B/C Branch Reconciliation Decision except `RECONCILE-CONFLICT`.
    1 — unexpected error (git not a repo, fetch failure, etc. — the bash
        oracle's `set -euo pipefail` propagates the failing git command's
        own exit code; this port does the same).

Behavior-preservation notes (read alongside the bash source):
  - `git fetch origin main` — its combined stdout+stderr is always echoed
    to OUR stderr (bash oracle line: `git fetch origin main >&2`),
    regardless of success. On failure, this port propagates the fetch
    command's own returncode (bash `set -e` semantics), not a hardcoded 1.
  - `git branch --show-current` failure is likewise fail-loud, hard-propagated
    (bash `set -e` semantics on `CURRENT=$(...)`).
  - `git merge-base --is-ancestor origin/main HEAD` — ONLY its zero/nonzero
    exit is inspected (any nonzero, including a genuine merge-base error,
    is treated as "needs reconcile" — the bash oracle does not distinguish
    "not an ancestor" from "merge-base error" here either; faithfully
    reproduced, not "fixed").
  - Both merge attempts (`--ff-only`, then `--no-ff`) are run with
    `COORDINATOR_OVERRIDE_BRANCH=1` + a scoped
    `COORDINATOR_OVERRIDE_BRANCH_REASON` in the subprocess environment,
    matching the oracle's per-attempt env-var scoping exactly (the reason
    string differs between the two attempts).
  - On a conflicted `--no-ff` merge, `git merge --abort` is run and its
    own exit code is unconditionally swallowed (bash oracle:
    `git merge --abort >&2 || true`) — this port does the same.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from coordinator_core.git.git_dir import resolve_git_dir
from coordinator_core.git.git_state import IndexParseError, read_index
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.win_portability import no_console_creationflags


def _run(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        env=env,
        **no_console_creationflags(),
    )


def _echo_to_stderr(proc: subprocess.CompletedProcess) -> None:
    if proc.stdout:
        sys.stderr.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)


def _merge_in_progress(repo_root: Path) -> bool:
    """Whether `MERGE_HEAD` is present in this worktree's private gitdir —
    read via `resolve_git_dir` (spawn-free by its own docstring), never via
    a `git rev-parse --git-path` spawn. Matches
    `push_failure_verdict::_merge_head_present`'s resolution shape."""
    try:
        return (resolve_git_dir(repo_root) / "MERGE_HEAD").exists()
    except (OSError, ValueError):
        # `resolve_git_dir` reads the `.git` pointer file as UTF-8; a
        # non-UTF-8 pointer file raises `UnicodeDecodeError` (a `ValueError`
        # subclass), not `OSError` — caught here too.
        return False


def _index_readable(repo_root: Path) -> bool:
    """Whether `.git/index` parses cleanly — one `read_index(fresh=True)`
    call, zero spawns. `fresh=True` because the index was mutated by the
    merge subprocess microseconds earlier; a cached read would be stale.
    Any `IndexParseError`, for any of its reasons, reads as *not readable*
    — deliberately conservative (see module Design table / docstring)."""
    try:
        read_index(repo_root, fresh=True)
        return True
    except IndexParseError:
        return False


def _classify_failed_merge(merge_in_progress: bool, index_readable: bool) -> str:
    """Three-way discrimination of a failed `--no-ff` merge, MERGE_HEAD-first:

    | merge_in_progress | index_readable | outcome |
    |---|---|---|
    | True  | False (raised) | RECONCILE-CONFLICT (unchanged, today's arm) |
    | True  | True           | RECONCILE-MERGE-COMMIT-REFUSED |
    | False | either         | RECONCILE-MERGE-NOT-STARTED |

    Pure, no I/O — the two probes above do the I/O and pass their booleans
    in."""
    if not merge_in_progress:
        return "RECONCILE-MERGE-NOT-STARTED"
    if index_readable:
        return "RECONCILE-MERGE-COMMIT-REFUSED"
    return "RECONCILE-CONFLICT"


def main(argv: list[str]) -> int:
    try:
        fetch = _run(["fetch", "origin", "main"])
    except FileNotFoundError:
        print("workday-start-step0-reconcile: git not found on PATH", file=sys.stderr)
        return 127
    _echo_to_stderr(fetch)
    if fetch.returncode != 0:
        return fetch.returncode

    show_current = _run(["branch", "--show-current"])
    if show_current.returncode != 0:
        _echo_to_stderr(show_current)
        return show_current.returncode
    current = show_current.stdout.strip()

    is_ancestor = _run(["merge-base", "--is-ancestor", "origin/main", "HEAD"])
    if is_ancestor.returncode == 0:
        print(f"ALREADY-CURRENT branch={current}")
        return 0

    ff_env = dict(os.environ)
    ff_env["COORDINATOR_OVERRIDE_BRANCH"] = "1"
    ff_env["COORDINATOR_OVERRIDE_BRANCH_REASON"] = (
        "workday-start step 0 reconcile origin/main (ff)"
    )
    ff_merge = _run(["merge", "--ff-only", "origin/main"], env=ff_env)
    if ff_merge.returncode == 0:
        print(f"RECONCILED-FF branch={current}")
        return 0

    merge_env = dict(os.environ)
    merge_env["COORDINATOR_OVERRIDE_BRANCH"] = "1"
    merge_env["COORDINATOR_OVERRIDE_BRANCH_REASON"] = (
        "workday-start step 0 reconcile origin/main (merge)"
    )
    no_ff_merge = _run(
        [
            "merge",
            "--no-ff",
            "origin/main",
            "-m",
            f"reconcile origin/main into {current} (workday-start)",
        ],
        env=merge_env,
    )
    _echo_to_stderr(no_ff_merge)
    if no_ff_merge.returncode == 0:
        print(f"RECONCILED-MERGE branch={current}")
        return 0

    # Both probes MUST be read strictly before the abort below — `git merge
    # --abort` clears unmerged index entries and removes MERGE_HEAD, and a
    # probe placed after it would read clean every time, inverting the
    # discrimination to "always RECONCILE-MERGE-NOT-STARTED".
    repo_root = Path(show_toplevel() or Path.cwd())
    merge_in_progress = _merge_in_progress(repo_root)
    index_readable = _index_readable(repo_root)
    outcome = _classify_failed_merge(merge_in_progress, index_readable)

    abort = _run(["merge", "--abort"])
    _echo_to_stderr(abort)
    print(f"{outcome} branch={current}")
    if outcome == "RECONCILE-CONFLICT":
        # Byte-identical to today's text (AC3) — the conservative-arm note
        # below is an ADDITION, never a replacement of this line.
        print(
            "Reconcile with origin/main hit a conflict — surface A/B/C Branch "
            "Reconciliation Decision.",
            file=sys.stderr,
        )
        if not index_readable:
            # The conservative arm: `.git/index` could not be parsed, so this
            # outcome also covers "genuinely unparseable for a non-conflict
            # reason" (e.g. a core.splitIndex box, where this arm fires on
            # every failed merge). Named explicitly rather than silently
            # folded into "conflict" so the degradation is visible.
            print(
                "The index could not be read to confirm this is a genuine "
                "content conflict; treated conservatively as one.",
                file=sys.stderr,
            )
    elif outcome == "RECONCILE-MERGE-COMMIT-REFUSED":
        print(
            "Reconcile with origin/main applied but was refused at the commit "
            "step (commonly a commit hook) — this is NOT the A/B/C Branch "
            "Reconciliation Decision.",
            file=sys.stderr,
        )
    else:  # RECONCILE-MERGE-NOT-STARTED
        print(
            "Reconcile with origin/main's merge never started — this is NOT "
            "the A/B/C Branch Reconciliation Decision.",
            file=sys.stderr,
        )
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
