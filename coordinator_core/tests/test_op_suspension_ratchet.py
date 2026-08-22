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
      Both directions are asserted EXPLICITLY and separately — see
      `test_roster_only_shrinks` and `test_reinstated_ops_are_pruned_from_the_floor`.
      Until 2026-08-22 only the growth direction had a test of its own, and a removal
      was caught incidentally by the behaviour tests below, which parametrised over
      this floor rather than over the live roster. That is why the first reinstatement
      this table ever saw (`hooks.cater_subagent_start`, 65bbe1323) left the guard red
      for a day: the op was correctly lifted, the floor was not pruned, and the two
      resulting failures read as "a suspended op reached its handler" — a guard-leak
      message for a bookkeeping omission. A reinstatement is the transition this file
      exists to police, so it must fail with the sentence that names the actual edit.
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
#
# A REMOVAL lands here in the same commit too, and that direction is the one this file
# got wrong once already: `hooks.cater_subagent_start` was lifted from SUSPENDED_OPS at
# 65bbe1323 on a properly measured cold case (1 spawn -> 0, 390.6ms -> 93.8ms import CPU)
# and this frozenset was not pruned, leaving the guard red until 2026-08-22.
_RATIFIED_SUSPENSIONS = frozenset({
    "ceremony.scoped_git_commit",
    "ceremony.wsc_tail",
    "session.sweep_consumed_handoffs",
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
    # hooks.cater_subagent_start — REINSTATED 65bbe1323, pruned here 2026-08-22.
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


def test_reinstated_ops_are_pruned_from_the_floor():
    """The removal direction, asserted in its own right rather than incidentally.

    An op leaving SUSPENDED_OPS is the campaign's goal, and it is a PM-visible fact
    exactly like an op entering: the floor above is the record of what was ratified,
    so it has to lose the name in the same commit the table does. Nothing here argues
    the removal was wrong — this test cannot tell a reinstatement from a deletion and
    should not try. It asserts only that the bookkeeping landed with it.

    Why this is a separate test rather than a second assert inside
    `test_roster_only_shrinks`: the two directions fail for opposite reasons and want
    opposite remedies (ratify the addition vs prune the removal), and a reader hitting
    one should not have to work out which half of a combined message applies.
    """
    live = set(op_budget_suspension.SUSPENDED_OPS)
    stale = _RATIFIED_SUSPENSIONS - live
    assert not stale, (
        f"Ops left SUSPENDED_OPS without being pruned from _RATIFIED_SUSPENSIONS: "
        f"{sorted(stale)}. If they earned their way back or were deleted, drop them "
        f"from the floor in the same commit — the floor is the ratified record, not a "
        f"historical archive. This is bookkeeping, not a guard leak."
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


# The BEHAVIOUR tests below parametrise over the LIVE roster, not over the ratified
# floor. What they assert is "an op that is off is refused", which is a claim about
# SUSPENDED_OPS; pointing them at the floor made them assert "an op that WAS off is
# still off", which is false by design the moment one earns its way back — and made a
# legitimate reinstatement look like a hole in the guard. Removal bookkeeping is
# `test_reinstated_ops_are_pruned_from_the_floor`'s job, and only its job.
@pytest.mark.parametrize("op", sorted(op_budget_suspension.SUSPENDED_OPS))
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


@pytest.mark.parametrize("op", sorted(op_budget_suspension.SUSPENDED_OPS))
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
    """Register guard: the message states the fact, optionally the caller's
    sanctioned fallback, and the cold bar — never a way around the refusal.

    docs/wiki/guard-messaging.md § Register B6 — a guard that names its own bypass
    argues against itself. This test previously carried the rationale "there is no
    bypass here, so there is nothing to name". That is still true of BYPASSES and
    is no longer the whole story: PM ruling 2026-08-21 admits a sanctioned
    FALLBACK (plain `git commit`) for `ceremony.scoped_git_commit`, on the ground
    that a cheaper mechanism preserving the op's guarantee defeats nothing. The
    banned-token list below is what keeps the two apart, so it is the part of this
    test that must not soften.
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


def test_commit_op_refusal_names_the_callers_own_path():
    """PM ruling 2026-08-21: plain `git commit` is the sanctioned fallback while
    `ceremony.scoped_git_commit` is off.

    The defect this pins is not a missing sentence, it is a wrong AUDIENCE. The
    refusal used to address only a future reinstatement case, so the caller that
    actually reads it — mid-workflow, blocked now — was left to invent its own
    disposition, and did: a commit agent reported "wait for the infrastructure to
    recover", naming an event a hand-curated table cannot emit.
    """
    message = op_budget_suspension.refusal_message("ceremony.scoped_git_commit")

    assert "git commit" in message
    assert "prepare-commit-msg" in message
    assert message.index("git commit") < message.index("Prove it under"), (
        "the caller's own path must come before the reinstatement bar -- that is "
        "the order the blocked caller reads in"
    )


def test_rows_without_a_sanctioned_fallback_name_none():
    """The empty slot is the correct answer for most of this table, and inventing
    a plausible-sounding fallback is the same improvisation the field prevents.

    Only ops with a real caller-drivable mechanism carry one. Every other row's
    refusal is unchanged: fact, then the cold bar.
    """
    for op, record in op_budget_suspension.SUSPENDED_OPS.items():
        if record.get("fallback"):
            continue
        message = op_budget_suspension.refusal_message(op)
        head = f"{op} is off: measured max "
        assert message.startswith(head), message
        assert message.endswith(
            "Prove it under 2s without the warm engine to bring it back."
        ), message
