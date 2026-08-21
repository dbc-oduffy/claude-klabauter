"""The op-suspension bar is a ratchet: it may be lowered, never raised.

Guards the PM's 2026-08-21 ruling that any op measured over 2s max is turned off,
and that an op comes back only by proving itself under 2s WITHOUT the warm engine.

The shape is deliberately `test_ceremony_budget_ratchet.py`'s: a SECOND, INDEPENDENT
literal of the bar lives here, so lifting `SUSPENSION_BAR_MS` in the source is not
enough to make the tree green. An edit that widens the bar has to widen it twice, in
two files, with two different rationales — which is the point at which it stops being
a quiet retune and becomes a visible argument with the PM.

Negative-spec:
    - Asserts the bar is not RAISED. Lowering is always legal and is never asserted
      against; that direction is the campaign's goal, not its risk.
    - Asserts the table only SHRINKS against a pinned floor of names. An op may be
      removed (it earned its way back, or it was deleted); the guard's job is to stop
      one being removed silently, so removals must come with this list edited.
    - Does NOT assert any op's measured numbers. Those are evidence for a reader, not
      a threshold — pinning them would make re-measuring an op a test failure.
    - Does not exercise timing. A test that measured latency here would be measuring
      the test box under whatever load it happens to carry, which DR-344 forbids
      resting a conclusion on.
"""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core import ipc, op_budget_suspension


# The second independent literal. Deliberately NOT imported from the source module.
_RATCHET_BAR_MS = 2000.0

# The suspension roster as ratified 2026-08-21. This list may lose entries as ops earn
# their way back or are deleted. It must never gain one silently — a new suspension is
# a new PM-visible fact, and it lands here in the same commit that lands the entry.
_RATIFIED_SUSPENSIONS = frozenset({
    "ceremony.scoped_git_commit",
    "ceremony.wsc_tail",
    "session.sweep_consumed_handoffs",
    "artifact.emit",
    "probes.fork_census",
    "session.boot_sweep",
    "queue.close",
    "memo.send",
    "fleet.archive_completed_plans",
    "fleet.archive_completed_handoffs",
    "review_trail.write",
    "fleet.archive_actioned_memos",
    "op_census.report",
    "completion.reconcile_commits",
    "hooks.cater_subagent_start",
    "hooks.track_touched_files",
    "testing.full_runner",
})


def test_bar_is_never_raised():
    assert op_budget_suspension.SUSPENSION_BAR_MS <= _RATCHET_BAR_MS, (
        "The suspension bar was raised. It is a ratchet: lower it or leave it. "
        "An op that does not fit is turned off, not accommodated."
    )


def test_roster_only_shrinks():
    live = set(op_budget_suspension.SUSPENDED_OPS)
    added = live - _RATIFIED_SUSPENSIONS
    assert not added, (
        f"Ops suspended without ratification: {sorted(added)}. Add them to "
        f"_RATIFIED_SUSPENSIONS in the same commit, so the roster's growth is visible."
    )


def test_every_entry_carries_its_measured_evidence():
    for op, record in op_budget_suspension.SUSPENDED_OPS.items():
        measured = record.get("measured")
        assert isinstance(measured, dict), f"{op}: no measured evidence"
        max_ms = measured.get("max_ms")
        assert isinstance(max_ms, (int, float)), f"{op}: max_ms is not a number"
        assert max_ms > _RATCHET_BAR_MS, (
            f"{op} is suspended but its recorded max ({max_ms}ms) is within the "
            f"{_RATCHET_BAR_MS}ms bar. Either the evidence is stale or the op should "
            f"be reinstated — a suspension with no breach behind it is a dial in disguise."
        )


@pytest.mark.parametrize("op", sorted(_RATIFIED_SUSPENSIONS))
def test_suspended_op_is_refused_at_dispatch(op):
    """The refusal is at dispatch, not in the handler — it must not need the op to load."""
    ipc.allow_unstamped_dispatch()
    response = asyncio.run(ipc.dispatch_message({
        "jsonrpc": "2.0",
        "id": 1,
        "method": op,
        "_origin_worktree": ".",
        "params": {},
    }))
    error = response.get("error")
    assert error is not None, f"{op} dispatched instead of being refused"
    assert error["code"] == ipc.OP_SUSPENDED_ERROR, (
        f"{op} was refused with {error['code']}, not OP_SUSPENDED_ERROR. A suspended "
        f"op must be distinguishable from a missing one."
    )


@pytest.mark.parametrize("op", sorted(_RATIFIED_SUSPENSIONS))
def test_suspended_op_cannot_be_resolved_for_in_process_invocation(op):
    """The OTHER door. Dispatch is not the only way an op gets invoked here.

    `get_op_handler(name)` returning a callable the caller awaits directly is a
    production pattern across this tree (`warm/entry_seam.py` names it "path 3").
    Guarding only `dispatch_message` left `ceremony.scoped_git_commit` and
    `review_trail.write` — the two most expensive ops in the roster — reachable at
    full cost from `safe_commit_offer.py` and `tail_ops.py`. This test is the
    regression guard for that gap, and it is why the roster is enforced in
    `get_op_handler` rather than only at the dispatch chokepoint.
    """
    with pytest.raises(op_budget_suspension.OpSuspendedError):
        ipc.get_op_handler(op)


def test_live_op_still_resolves():
    """The guard must not have turned `get_op_handler` into a refusal for everyone."""
    assert ipc.get_op_handler("ping") is not None


def test_refusal_never_offers_a_way_around_it():
    """Register guard: the message states the fact and the cold bar, nothing else.

    docs/wiki/guard-messaging.md § Register B6 — a guard that names its own bypass
    argues against itself. There is no bypass here, so there is nothing to name, and
    this test exists to keep it that way.
    """
    banned = ("env", "COORDINATOR_", "override", "bypass", "disable", "skip",
              "raise the timeout", "increase")
    for op in op_budget_suspension.SUSPENDED_OPS:
        message = op_budget_suspension.refusal_message(op)
        lowered = message.lower()
        for token in banned:
            assert token.lower() not in lowered, (
                f"{op}'s refusal names {token!r}: {message!r}"
            )
        assert "without the warm engine" in lowered, (
            f"{op}'s refusal omits the cold-measurement bar, which is the only route back"
        )
