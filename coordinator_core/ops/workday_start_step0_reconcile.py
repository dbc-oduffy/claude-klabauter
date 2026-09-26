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
    3 — a failed `--no-ff` merge, discriminated by `_classify_failed_merge`
        into one of three outcome strings: a genuine content conflict
        (`RECONCILE-CONFLICT`, PM resolves first via the A/B/C Branch
        Reconciliation Decision), the merge applying but its commit being
        refused (`RECONCILE-MERGE-COMMIT-REFUSED`, commonly a commit hook —
        not an A/B/C decision), or the merge never starting at all
        (`RECONCILE-MERGE-NOT-STARTED` — dirty worktree, unrelated
        histories, or a bad ref; also not an A/B/C decision).
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
    """Whether MERGE_HEAD exists in THIS worktree's private gitdir.

    Resolved via `resolve_git_dir` (worktree-private gitdir, spawn-free per
    its own docstring), matching the precedent this plan cites
    (`push_failure_verdict :: _merge_head_present(repo_root)`). A named
    module-level callable, not an inline expression, so AC8's monkeypatch
    has a name in this module's namespace to bind to.
    """
    return (resolve_git_dir(repo_root) / "MERGE_HEAD").exists()


def _index_readable(repo_root: Path) -> bool:
    try:
        read_index(repo_root, fresh=True)
    except IndexParseError:
        return False
    return True


def _classify_failed_merge(merge_in_progress: bool, index_readable: bool) -> str:
    """Discriminate a failed `--no-ff` merge into one of three outcome
    strings, per the Design table
    (docs/plans/2026-09-06-engine-publish-lag-hook-gen-forwarder-regen.md).

    Pure, no I/O -- both probes are resolved by the caller and passed in,
    which is what makes this function's fast, zero-spawn unit test
    possible.

    | merge_in_progress | index_readable | outcome |
    |---|---|---|
    | True  | False (raised) | `RECONCILE-CONFLICT` (unchanged, today's meaning) |
    | True  | True           | `RECONCILE-MERGE-COMMIT-REFUSED` |
    | False | either         | `RECONCILE-MERGE-NOT-STARTED` |

    None of the three strings names a cause -- `RECONCILE-MERGE-COMMIT-
    REFUSED` claims only that the merge applied and reached the commit step
    with no unmerged entry in the index; a hook abort is the known cause and
    may be named in the caller's stderr hint, never here.
    """
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

    # --abort` clears unmerged index entries and removes MERGE_HEAD, and a
    # discrimination to "always RECONCILE-MERGE-NOT-STARTED".
    repo_root = Path(show_toplevel() or Path.cwd())
    merge_in_progress = _merge_in_progress(repo_root)
    index_readable = _index_readable(repo_root)
    outcome = _classify_failed_merge(merge_in_progress, index_readable)

    abort = _run(["merge", "--abort"])
    _echo_to_stderr(abort)
    print(f"{outcome} branch={current}")
    if outcome == "RECONCILE-CONFLICT":
        # below is an ADDITION, never a replacement of this line.
        print(
            "Reconcile with origin/main hit a conflict — surface A/B/C Branch "
            "Reconciliation Decision.",
            file=sys.stderr,
        )
        print(
            "The index could not be confirmed readable when this was "
            "classified — on a core.splitIndex box this arm fires for "
            "every failed merge, not only a genuine conflict.",
            file=sys.stderr,
        )
    elif outcome == "RECONCILE-MERGE-COMMIT-REFUSED":
        print(
            "Reconcile with origin/main applied but the merge commit was "
            "refused (commonly a commit hook) — this is not the A/B/C "
            "Branch Reconciliation Decision.",
            file=sys.stderr,
        )
    else:
        print(
            "Reconcile with origin/main's merge never started (dirty "
            "worktree, unrelated histories, or a bad ref) — this is not "
            "the A/B/C Branch Reconciliation Decision.",
            file=sys.stderr,
        )
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
