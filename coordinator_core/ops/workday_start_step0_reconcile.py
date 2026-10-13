"""
coordinator_core.ops.workday_start_step0_reconcile — Step 0.4.5 (Reconcile
with origin/main) for the `/workday-start` precedence-switch flow.

Purpose: DR-059 bash-to-naked-Python port of the DoE-owned reconcile script.
Called by the /workday-start Step 0 flow after the branch-precedence switch
resolves on a non-main active branch. Folds `origin/main` into the current
branch so the active workstream stays mergeable; never abandons in-progress
work.

Spec backlink: coordinator-content-repo:pln-bash-to-naked-python-engine-mi-c09292
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
  - Before the merge attempts, ONE `git diff --cached --name-only -z` reads
    the staged path set (the single added spawn). Empty: nothing changes.
    Every path under `state/block-discharge/`: committed by one `git add`
    plus one `git commit`, each carrying every path as an explicit
    pathspec, then `SPOOL-COMMITTED <n>` is printed and the merge proceeds.
    Any other staged path: nothing is committed, the outcome is
    `RECONCILE-MERGE-NOT-STARTED` (exit 3) and the staged paths follow a
    `STAGED-PATHS` stderr line, one per line. A `RECONCILE-CONFLICT` lists
    its conflicted paths after a `CONFLICT-PATHS` stderr line.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from coordinator_core.contract.apply_base import _mirror_session_env_for_subprocess
from coordinator_core.git.git_dir import resolve_git_dir
from coordinator_core.git.git_state import IndexParseError, read_index
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.git_lock_retry import run_with_lock_retry
from coordinator_core.session.record_homes import home_dir
from coordinator_core.win_portability import no_console_creationflags


_BLOCK_DISCHARGE_SPOOL_ROOT = Path(home_dir(".", "block-discharge")).as_posix() + "/"
_SPOOL_COMMIT_MESSAGE = "block-discharge: commit staged spool so reconcile can merge"
_CONFLICT_LINE = re.compile(
    r"^CONFLICT \([^)]*\): (?:Merge conflict in (?P<merge>.+)"
    r"|(?P<other>.+?) (?:deleted|modified|added|renamed|left)\b.*)$"
)


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


def _staged_paths() -> tuple[int, list[str], str]:
    """(returncode, staged paths, stderr) from one `git diff --cached
    --name-only -z`; sees staged deletions, which an index-driven read cannot."""
    proc = _run(["diff", "--cached", "--name-only", "-z"])
    paths = [p for p in proc.stdout.split("\0") if p]
    return proc.returncode, paths, proc.stderr


def _print_staged_paths(staged: list[str]) -> None:
    print("STAGED-PATHS", file=sys.stderr)
    for path in staged:
        print(path, file=sys.stderr)


def _commit_spool(paths: list[str]) -> subprocess.CompletedProcess | None:
    """Commit exactly `paths` (one add, one commit, explicit pathspecs).
    Returns the failing process, or None on success."""
    with _mirror_session_env_for_subprocess():
        add = run_with_lock_retry(lambda: _run(["add", "--", *paths]))
    if add.returncode != 0:
        return add
    with _mirror_session_env_for_subprocess():
        commit = run_with_lock_retry(
            lambda: _run(["commit", "-m", _SPOOL_COMMIT_MESSAGE, "--", *paths])
        )
    return commit if commit.returncode != 0 else None


def _conflict_paths(merge_output: str) -> list[str]:
    found: list[str] = []
    for line in merge_output.splitlines():
        match = _CONFLICT_LINE.match(line.strip())
        if match:
            found.append((match.group("merge") or match.group("other")).strip())
    return found


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

    staged_rc, staged, staged_err = _staged_paths()
    if staged_rc != 0:
        sys.stderr.write(staged_err)
        return staged_rc
    spool_committed = 0
    if staged:
        if not all(p.startswith(_BLOCK_DISCHARGE_SPOOL_ROOT) for p in staged):
            print(f"RECONCILE-MERGE-NOT-STARTED branch={current}")
            print(
                "Reconcile with origin/main's merge never started (staged "
                "paths outside the block-discharge spool) — this is not the "
                "A/B/C Branch Reconciliation Decision.",
                file=sys.stderr,
            )
            _print_staged_paths(staged)
            return 3
        failed = _commit_spool(staged)
        if failed is not None:
            _echo_to_stderr(failed)
            print(f"RECONCILE-MERGE-NOT-STARTED branch={current}")
            print(
                "Reconcile with origin/main's merge never started (the "
                "staged spool could not be committed) — this is not the "
                "A/B/C Branch Reconciliation Decision.",
                file=sys.stderr,
            )
            _print_staged_paths(staged)
            return 3
        spool_committed = len(staged)
        print(f"SPOOL-COMMITTED {spool_committed}")

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
        print(
            "The index could not be confirmed readable when this was "
            "classified — on a core.splitIndex box this arm fires for "
            "every failed merge, not only a genuine conflict.",
            file=sys.stderr,
        )
        print("CONFLICT-PATHS", file=sys.stderr)
        for path in _conflict_paths(no_ff_merge.stdout + "\n" + no_ff_merge.stderr):
            print(path, file=sys.stderr)
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
