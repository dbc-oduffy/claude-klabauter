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
    3 — merge conflict; merge aborted, PM resolves first
        (`RECONCILE-CONFLICT`).
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

    abort = _run(["merge", "--abort"])
    _echo_to_stderr(abort)
    print(f"RECONCILE-CONFLICT branch={current}")
    print(
        "Reconcile with origin/main hit a conflict — surface A/B/C Branch "
        "Reconciliation Decision.",
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
