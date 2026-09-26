"""
coordinator_core.hooks.day_branch_assert — engine-side boot assert making the
day-branch cut a property of the TREE rather than of EM discipline.

Chunks C4b (dispatch), C5 (banner mechanism), and C10 (case-(B) detection and
warn) of DoE-claude ``docs/plans/2026-08-18-enforce-day-branch-cut-tree-invariant.md``,
delivered here by cross-repo memo
``2026-08-18-doe-claude-em-day-branch-cut-tree-invariant-engine-work.md``.

Split rationale: the DoE SessionStart fan-in loads guards BY FILENAME from its
own directory, so a claude-klabauter-resident module is structurally unreachable from
that loader. DoE ships the shim (`coordinator/hooks/scripts/day-branch-assert.py`,
chunk C4a) which imports and calls `assert_day_branch` here. Hosting and the
firing set are DoE-plane doctrine; the git logic is engine-plane and lives here.

Authorising ruling (PM, 2026-08-18): "we cut automatically if we're on main. we
warn if we are on a branch that is not compliant with our auto-push rules."

    Case (A) — tree on ``main``: cut automatically. No ask, no PM gate, no EM
    judgment.
    Case (B) — non-``main`` and non-compliant with the auto-push rules: WARN.
    Do not cut, do not switch.

Everything else — mid-execution switches, checkout of a different commit,
rename-with-remote-delete — stays a PM-gated ask, unchanged.

    Negative-spec — boot cost is load-bearing, not a nicety. A SessionStart
    branch-cutting hook existed here before (`session-ensure-branch.sh`,
    2026-07-05) and was retired nine days later, followed by a PM directive of
    2026-07-15 stripping ALL boot-time guardrail SessionStart hooks fleet-wide.
    That was a BOOT-COST trim (Windows spawn tax), not a recorded correctness
    failure — no lesson or incident record names a behavioural defect in the
    retired hook. This restores the shape under the new ruling and stays inside
    the directive's constraint: the added boot cost is ONE
    ``git branch --show-current`` on the early-return path, plus on the rare
    lock-winner path a local ``checkout -b``. Do not grow this leg past that
    without fresh PM assent.

    Negative-spec — this must NEVER be designed to fire mid-session. The DoE
    side pins the guard to ``sources=frozenset({"startup"})``: ``compact``,
    ``resume`` and ``fork`` all fire in a session that is already mid-execution,
    and a cut on ``compact`` is precisely the mid-execution mutation the
    doctrine keeps out of bounds.

    Negative-spec — the banner is NON-SUPPRESSIBLE. No quiet flag, no
    once-per-day sentinel. A guard that announces itself once and then goes
    quiet is the exact failure this workstream exists to fix. Elapsed time is
    IN the banner so a state persisting for hours reads differently from one a
    minute old.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import NamedTuple, Optional


def _is_cloud_session() -> bool:
    return (os.environ.get("CLAUDE_CODE_REMOTE") or "").strip().lower() == "true"


CUT = "FRESH-CUT"
ADOPTED = "ADOPTED-EXISTING"
#: to HEAD and checked out. See `session_ensure_branch.ADVANCED_TO_HEAD` for
ADVANCED = "ADVANCED-TO-HEAD"
INHERITED = "INHERITED"
COMPLIANT = "COMPLIANT"
WARN = "WARN"
FAILED = "FAILED"


class DayBranchAssertResult(NamedTuple):
    """What the assert did, and the operator-facing line (if any).

    outcome: one of CUT / ADOPTED / INHERITED / COMPLIANT / WARN / FAILED.
    branch:  the branch the tree is on afterwards ("" when unresolvable).
    message: the line to print, or "" for the silent COMPLIANT arm. Callers
      print it verbatim on the boot-banner channel — the shim does not
      re-render, re-word, or suppress it.
    """

    outcome: str
    branch: str
    message: str


_SESSION_ENSURE_BRANCH_PATH = (
    Path(__file__).resolve().parents[2] / "coordinator" / "lib" / "session_ensure_branch.py"
)
_session_ensure_branch = None


def _load_session_ensure_branch():
    global _session_ensure_branch
    if _session_ensure_branch is None:
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location(
            "_day_branch_assert_session_ensure_branch", _SESSION_ENSURE_BRANCH_PATH
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {_SESSION_ENSURE_BRANCH_PATH}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _session_ensure_branch = module.session_ensure_branch
    return _session_ensure_branch


def assert_day_branch(
    repo_root: str,
    machine: str,
    today: str,
    *,
    env: Optional[dict] = None,
    stderr=None,
) -> DayBranchAssertResult:
    """The boot invariant. EXPORTED SIGNATURE — DoE's C4a shim calls this.

    Dispatch table, exhaustive:

      ``main``                          -> case (A): cut / adopt-existing /
                                           advance-to-HEAD / inherit, inside
                                           the tree-keyed cut lock, with no
                                           network call.
      detached HEAD                     -> case (B): warn (always
                                           non-compliant).
      non-``main``, auto-push compliant -> silent return, zero further work.
      non-``main``, non-compliant       -> case (B): warn.

    The entire non-``main`` arm is owned by :func:`case_b_verdict` (C10); this
    function does not also implement a competing early return for it.

    Designated-day-branch short-circuit (PM ruling 2026-09-22): when
    `coordinator.dayBranch` is set (cloud pre-boot's own record of the
    harness-designated branch) and the tree already sits on it, the invariant
    already holds — silent COMPLIANT, ahead of the main/non-main dispatch, no
    cut and no warn, whatever the branch's shape.
    """
    branch = _current_branch(repo_root)

    from coordinator_core.daily_branch import (
        read_configured_day_branch,
        record_day_branch_designation,
    )

    configured = read_configured_day_branch(repo_root)
    if configured is None and _is_cloud_session() and branch and branch != "main":
        if record_day_branch_designation(repo_root, branch):
            configured = branch
    if configured and branch == configured:
        return DayBranchAssertResult(COMPLIANT, branch, "")

    if branch == "main":
        return _case_a(repo_root, machine, today, env=env, stderr=stderr)

    return case_b_verdict(repo_root, branch)


def _case_a(repo_root, machine, today, *, env, stderr) -> DayBranchAssertResult:
    session_ensure_branch = _load_session_ensure_branch()

    result = session_ensure_branch(
        machine,
        today,
        "main",
        "no",
        0,
        env=env,
        stderr=stderr,
        caller="boot",
    )

    if result.result == "FRESH-CUT":
        return DayBranchAssertResult(
            CUT, result.new_branch, f"day-branch: cut {result.new_branch}"
        )
    if result.result == "ADOPTED-EXISTING":
        return DayBranchAssertResult(
            ADOPTED, result.new_branch, f"day-branch: on {result.new_branch}"
        )
    if result.result == "ADVANCED-TO-HEAD":
        return DayBranchAssertResult(
            ADVANCED, result.new_branch, f"day-branch: advanced {result.new_branch} to HEAD"
        )
    if result.result == "INHERITED":
        return DayBranchAssertResult(
            INHERITED, result.new_branch, f"day-branch: inherited {result.new_branch}"
        )

    return DayBranchAssertResult(
        FAILED,
        "main",
        banner(
            headline="day-branch NOT cut — tree is still on main",
            detail=(
                "the cut was attempted and failed; commits made now land on "
                "main, where auto-push provides no crash insurance"
            ),
            since=None,
        ),
    )


#: DOCTRINE, and they get one informational line, not the escalating banner.
_RECOGNIZED_LONG_LIVED = ("migration/", "release/", "feature/")


def case_b_verdict(repo_root: str, branch: str) -> DayBranchAssertResult:
    from coordinator_core.daily_branch import has_remote_prefix, is_work_branch

    if not branch:
        return DayBranchAssertResult(
            WARN,
            "",
            banner(
                headline="detached HEAD",
                detail=(
                    "auto-push cannot run and crash insurance is NOT in force; "
                    "commits made here are reachable only by SHA"
                ),
                since=None,
            ),
        )

    if has_remote_prefix(branch):
        rename_target = branch[len("origin/"):]
        return DayBranchAssertResult(
            WARN,
            branch,
            (
                f"day-branch: {branch} carries a remote prefix (origin/); "
                f"rename with `git branch -m {branch} {rename_target}`."
            ),
        )

    if not is_work_branch(branch):
        if branch.startswith(_RECOGNIZED_LONG_LIVED):
            return DayBranchAssertResult(
                WARN,
                branch,
                (
                    f"day-branch: auto-push is off for {branch} by doctrine "
                    "(recognized long-lived workstream shape; work/* only). "
                    "Push manually if intended."
                ),
            )
        return DayBranchAssertResult(
            WARN,
            branch,
            banner(
                headline=f"{branch} is not a work/* branch",
                detail=(
                    "auto-push skips it (doctrine: work/* only), so crash "
                    "insurance is NOT in force for this branch"
                ),
                since=None,
            ),
        )

    return DayBranchAssertResult(COMPLIANT, branch, "")


def banner(*, headline: str, detail: str, since: Optional[float]) -> str:
    elapsed = ""
    if since is not None:
        secs = max(0.0, time.time() - since)
        elapsed = f" [{_humanize(secs)}]"
    return f"── day-branch{elapsed}: {headline} — {detail} ──"


def _humanize(secs: float) -> str:
    if secs < 90:
        return f"{int(secs)}s"
    if secs < 5400:
        return f"{int(secs // 60)}m"
    return f"{int(secs // 3600)}h"


def _current_branch(repo_root: str) -> str:
    from coordinator_core.hooks.auto_push import resolve_branch

    return resolve_branch(repo_root) or ""
