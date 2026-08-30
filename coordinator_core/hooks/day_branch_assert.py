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

import subprocess
import time
from pathlib import Path
from typing import NamedTuple, Optional


#: Every arm of the dispatch table below, named. Nothing falls off the end.
CUT = "FRESH-CUT"
ADOPTED = "ADOPTED-EXISTING"
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
                                           inherit, inside the tree-keyed cut
                                           lock, with no network call.
      detached HEAD                     -> case (B): warn (always
                                           non-compliant).
      non-``main``, auto-push compliant -> silent return, zero further work.
      non-``main``, non-compliant       -> case (B): warn.

    The entire non-``main`` arm is owned by :func:`case_b_verdict` (C10); this
    function does not also implement a competing early return for it.
    """
    branch = _current_branch(repo_root)

    if branch == "main":
        return _case_a(repo_root, machine, today, env=env, stderr=stderr)

    return case_b_verdict(repo_root, branch)


def _case_a(repo_root, machine, today, *, env, stderr) -> DayBranchAssertResult:
    # sys.path + flat import, mirroring workday-start-step0.py's own
    # `sys.path.insert(0, _LIB_DIR); from session_ensure_branch import ...`.
    # NOT `from coordinator.lib...`: `coordinator/` carries no __init__.py and
    # the name collides with the DoE-claude plugin root of the same name, so a
    # package import would resolve to whichever is on sys.path first.
    import sys

    lib_dir = str(Path(repo_root) / "coordinator" / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    from session_ensure_branch import session_ensure_branch

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


# ---------------------------------------------------------------------------
# C10 — case (B): the entire non-`main` arm.
# ---------------------------------------------------------------------------

#: `auto_push.branch_gate`'s own doctrine splits non-`work/*` branches into two
#: populations. Warning identically for both is nag-shaped and habituates away
#: from the genuinely loud cases, so the message is differentiated: these are
#: deliberate, legitimate, long-lived shapes for which auto-push is off BY
#: DOCTRINE, and they get one informational line, not the escalating banner.
_RECOGNIZED_LONG_LIVED = ("migration/", "release/", "feature/")

#: Gravestone -- the pending-push record leg (`coordinator-auto-push-pending.json`)
#: was removed here on 2026-08-30. It asserted "the last push attempt failed and
#: commits are sitting unpushed right now" from the presence of a file whose only
#: production writer (`auto_push._hold_window`, via `_write_pending_record`) C8 of
#: `docs/plans/2026-08-30-who-pushes-and-when.md` gravestoned. Post-C8 the record
#: can never be written again, so the leg could only ever fire on a pre-C8
#: orphan -- a permanent false RED on a maximally-trusted surface, which is what
#: it did: it told a peer session crash insurance was off box-wide while the
#: cadence was carrying every push. The other three legs below/above are NOT
#: stale: `push_outstanding` still consults `auto_push.branch_gate()` and still
#: declines `main`, a non-`work/*` branch, and an unresolvable HEAD.


def case_b_verdict(repo_root: str, branch: str) -> DayBranchAssertResult:
    """Non-``main``: compliant -> silent; otherwise the warn.

    "Violates the auto-push rules" is defined concretely against
    ``coordinator_core/hooks/auto_push.py``:

      - ``branch_gate`` (``work/*`` only): a branch that does NOT start with
        ``work/`` gets no auto-push at all, so no crash insurance.
    Compliant = ``work/*`` shape. A detached HEAD is always non-compliant and
    always warns.

    Negative-spec — do not reintroduce a pending-push-record leg here. See the
    gravestone above ``case_b_verdict``: the record has no writer post-C8, so
    reading it can only produce a false RED.
    """
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

    if not branch.startswith("work/"):
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


# ---------------------------------------------------------------------------
# C5 — the banner mechanism. ONE renderer, shared by every loud surface.
# ---------------------------------------------------------------------------


def banner(*, headline: str, detail: str, since: Optional[float]) -> str:
    """Render the loud, escalating, non-suppressible banner.

    Escalating means elapsed time is IN the text: a state persisting for hours
    must not read like one a minute old. ``since`` is a unix timestamp the
    state has held since, or None when it is not known.

    Every surface that needs this banner calls THIS function —
    ``/workweek-start``'s branch leg included. A second renderer printing
    similar-but-different text is the failure mode this signature exists to
    prevent: it is a mid-session slash command that may never re-enter the
    SessionStart hook, so its output must route through here, not a copy.
    """
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
    """The one git spawn on the early-return path. See the boot-cost
    negative-spec in this module's docstring before adding a second."""
    from coordinator_core.win_portability import no_console_creationflags

    proc = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo_root,
        capture_output=True,
        **no_console_creationflags(),
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", errors="replace").strip()
