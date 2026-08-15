"""Delivery oracle for the bare-commit escalation: does the verdict this
package computes actually REACH the operator.

Backlink: `state/bug-backlog/2026-08-15-bare-commit-deny-never-reached-the-
operator.yaml`. On 2026-08-15 a compound `git add -- <mine> && git commit -q
-F - <<EOF` swept 19 files staged by two peer sessions and printed NOTHING --
no deny, no advisory. Detection was never the defect: the check fires on that
exact string, heredoc and all (`test_git_commit_safe_commit_deny_escalation.
py` owns that axis). The transcript's own `hook_cancelled` attachment records
what happened instead -- `durationMs=16336`, `timeoutMs=15000`, `timedOut=
true`: the harness cancelled the whole PreToolUse hook mid-chain, so no
guard's verdict was ever emitted.

Two things this module pins, in the order the backlog entry asks them.

LEG 1 -- the band hypothesis, FALSIFIED. The backlog's unverified reading of
the registration line was that a deny returned from a `GuardBand.
ADVISORY_REWRITE` entry might be downgraded or dropped by the dispatcher.
It is not: bands sequence guards and nothing else, and the loop returns the
first non-`None` envelope whatever its shape. `test_advisory_band_deny_
reaches_the_caller` holds that open so the hypothesis is not re-derived from
the registration line a third time. It passes before and after the fix
below -- it is a pin, not the regression proof.

LEG 2 -- the real one, and the regression proof. Per-probe `timeout=2.0`
bounded ONE spawn; nothing bounded their sum, and a commit-shaped command
spawns six git processes on this path, so the engine's own worst case sat
ABOVE the window the harness cancels at. `_run_git`'s budget (`dispatch_
checks._git_probe_deadline`) is what makes the chain finish and deliver:
probes past the budget are declined unspawned, each falling back to its own
already-specified fail-open default, instead of the whole hook dying and
taking every guard's verdict with it.

Negative-spec: this module asserts nothing about WHICH verdict the commit
check computes (advisory vs deny under a given index) -- that is
`test_git_commit_safe_commit_deny_escalation.py`'s oracle -- and spawns no
real process, so it stays in the fast tier where a delivery regression is
seen the same day it lands.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards import dispatch_checks


#: The 2026-08-15 command, reduced to its shape: a scoped `git add`, a bare
#: `git commit` taking its subject on stdin, and a trailing read after the
#: heredoc terminator.
#:
#: The original also carried a `cd <repo>;` prefix, dropped here on purpose.
#: `check_offer_git_c` is registered EARLIER in the same band and returns a
#: deny of its own on that prefix whenever the `cd` target does not resolve
#: to the payload's `cwd`; since the loop returns the first non-`None`
#: envelope, keeping the prefix would make these rows assert on THAT guard's
#: verdict instead of the one under test. The preemption is real and is
#: reported as its own finding -- it is not this module's subject.
_SWEEP_SHAPE_CMD = (
    "git add -- a.py b.md && git commit -q -F - <<'EOF'\n"
    "subject line\n"
    "\n"
    "body paragraph\n"
    "EOF\n"
    "git log -1 --format=%H"
)


def _payload(cmd: str, session_id: str = "sess-delivery") -> str:
    return json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "session_id": session_id,
            "cwd": "",
        }
    )


class _FakeCompleted:
    """The two attributes `_run_git` reads off `subprocess.run`'s result."""

    def __init__(self, stdout: str = "") -> None:
        self.returncode = 0
        self.stdout = stdout


def _install_slow_git(monkeypatch, per_spawn_seconds: float) -> List[List[str]]:
    """Replace every `subprocess.run` this package reaches with a fake that
    costs `per_spawn_seconds` and records its argv. A real slow `git` cannot
    be arranged deterministically on a machine whose whole problem is that
    its load is not deterministic; the fake makes the cost the test needs to
    reason about the only cost there is.

    Returns the live argv log, so a caller can count what was spawned and --
    the point of the exercise -- what was not.
    """
    spawned: List[List[str]] = []

    def _fake_run(args, *_a: Any, **_kw: Any) -> _FakeCompleted:
        spawned.append(list(args))
        time.sleep(per_spawn_seconds)
        return _FakeCompleted()

    monkeypatch.setattr(dispatch_checks.subprocess, "run", _fake_run)
    return spawned


@pytest.fixture(autouse=True)
def _no_leaked_deadline():
    """A budget armed by a failing assertion mid-dispatch must not survive
    into the next test in this long-lived process -- the same stale-state
    hazard `_disarm_git_probe_deadline`'s own docstring names."""
    yield
    dispatch_checks._disarm_git_probe_deadline()


def test_advisory_band_deny_reaches_the_caller(monkeypatch) -> None:
    """LEG 1 pin: an `ADVISORY_REWRITE`-band entry's DENY is returned by the
    dispatcher verbatim, not downgraded to allow and not dropped.

    The escalation predicate is forced rather than staged into a real index:
    the claim under test is about the DISPATCHER's handling of a deny-shaped
    return, which is independent of how the predicate reached `True`.
    """
    monkeypatch.setattr(
        dispatch_checks, "_bt_c7_index_holds_foreign_paths", lambda *a, **k: True
    )
    out = dispatch.evaluate_payload_json(_payload(_SWEEP_SHAPE_CMD))
    assert isinstance(out, dict), (
        "the chain returned nothing for a deny-shaped verdict"
        if out is None
        else "collect_advisories was never requested; a list here means the "
        "dispatcher's single-envelope contract moved"
    )
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "OUTSIDE this command's own 'git add'" in hso["permissionDecisionReason"]


def test_probe_budget_declines_unspawned_once_spent(monkeypatch) -> None:
    """`_run_git` stops spawning once the armed budget is spent, and says so
    with the fail-OPEN return code rather than the fail-CLOSED timeout one.

    Fails before the fix: without a budget every call spawns, so the third
    call returns `(0, "")` and the log holds three entries.
    """
    spawned = _install_slow_git(monkeypatch, per_spawn_seconds=0.15)
    dispatch_checks._arm_git_probe_deadline(0.2)

    first = dispatch_checks._run_git(["rev-parse", "--show-toplevel"])
    second = dispatch_checks._run_git(["diff", "--cached", "--name-only"])
    third = dispatch_checks._run_git(["diff", "--cached", "--name-only", "--", "a.py"])

    assert first[0] == 0
    assert second[0] == 0
    assert third == (dispatch_checks._GIT_PROBE_BUDGET_SPENT_RC, "")
    assert third[0] != -1, (
        "budget exhaustion must not borrow the timeout sentinel: four oracles "
        "in dispatch_checks treat rc == -1 as fail-CLOSED and would manufacture "
        "a deny out of machine load"
    )
    assert len(spawned) == 2, "the declined probe still spawned: %r" % (spawned,)


def test_unarmed_budget_never_declines_a_probe(monkeypatch) -> None:
    """The budget is inert unless armed -- a check invoked directly (every
    other test in this package, `_alternative_liveness`'s harness) keeps
    today's behaviour byte-for-byte."""
    spawned = _install_slow_git(monkeypatch, per_spawn_seconds=0.05)
    assert dispatch_checks._git_probe_deadline is None

    for _ in range(6):
        assert dispatch_checks._run_git(["rev-parse", "--git-dir"])[0] == 0

    assert len(spawned) == 6


def test_dispatch_finishes_and_delivers_when_git_is_slow(monkeypatch, capsys) -> None:
    """LEG 2 regression: with git slow enough that the pre-fix chain would
    have spent its whole window in subprocesses, the dispatch still declines
    the overrunning probes and RETURNS -- the property the cancelled hook
    could not hold.

    Fails before the fix: nothing declines a probe, so no decline line is
    printed and every git call on this path is spawned.
    """
    monkeypatch.setattr(dispatch_checks, "_GIT_PROBE_BUDGET_SECONDS", 0.2)
    spawned = _install_slow_git(monkeypatch, per_spawn_seconds=0.15)

    out = dispatch.evaluate_payload_json(_payload(_SWEEP_SHAPE_CMD))

    declined = [
        line
        for line in capsys.readouterr().err.splitlines()
        if "git probe budget" in line
    ]
    assert declined, (
        "no probe was declined -- the chain spent its whole budget in "
        "subprocesses, which is what the harness cancels a hook for"
    )
    assert isinstance(out, dict), "the chain produced no envelope for the sweep shape"
    assert out["hookSpecificOutput"]["permissionDecision"] in ("allow", "deny")
    assert dispatch_checks._git_probe_deadline is None, "budget left armed"
    assert spawned, "the fake git was never reached; the shape stopped matching"
