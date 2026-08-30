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

§ Roster COMPLETENESS (added 2026-08-23, plan `2026-08-23-the-roster-stops-being-
hand-curated.md`, C4 -- AC11)

Everything above polices roster CHANGES: it never gains a row silently, and a
removal must prune the floor. It has nothing to say about roster COMPLETENESS --
whether every op the audit found admissible actually landed on the roster. That is
`test_roster_matches_most_recent_audit_content` and
`test_audit_is_not_stale_against_the_live_sink` below.

Both compare ARTIFACTS ON DISK, never a live measurement, per this same file's own
negative-spec above. Content and freshness are two SEPARATE assertions (AC11) --
a guard that only checks content is green whenever nobody re-runs the audit, which
reproduces this plan's own § Problem one artifact to the left. Both SKIP, not fail,
when the artifact they need is absent -- the three-state norm `op_census/timing.py`
already uses for missing telemetry, applied here to a missing or unreadable audit
document / sink (a fresh clone, a peer's box: `.git/coordinator-sessions/logs/` is
not a committed path, and this plan's audit record does not exist until C2/C5 land).

THE AUDIT RECORD'S MACHINE-READABLE CONTRACT, stated here because nothing else in
this plan's chunks has stated it yet (C4 is lifted ahead of C2/C5 per the plan's own
sequencing note, needing only C3's predicate signature -- not a finished audit). A
prose-only audit is not checkable by a guard that may not exercise timing and may
not re-derive a fact DR-306 requires named as a field
(`docs/wiki/computed-fact-in-prose-is-break-class.md`), so the audit MUST carry,
alongside its prose, one fenced ```yaml block titled `admission-audit` with:

    stamp:
      generation_paths: [<str>, ...]      # OccupancyStamp.generation_paths
      generation_byte_sizes: [<int>, ...] # OccupancyStamp.generation_byte_sizes
      total_rows_read: <int>              # OccupancyStamp.total_rows_read
    admitted:
      - op: <str>
        max_observed_ms: <float>
        occupancy_secs: <float>
        disposition: <str>      # one of: admissible | spared | stale_measurement | dead
        disposition_reason: <str|null>  # required for every disposition except admissible

`disposition` states which of four situations the row is in: `admissible` (over the
bar, live, correctly measured -- MUST be on the roster or this guard fails),
`spared` (genuinely admissible, but a PM-ratified reasoned decision not to
suspend), `stale_measurement` (the only qualifying row predates the op's own
fix, so the number measures code that no longer exists), or `dead` (the op
does not exist at HEAD -- admissibility is meaningless for it). Only
`admissible` rows drive this guard's completeness check; the other three
dispositions are carried on the record for completeness of the audit
obligation but never make this guard fail on their own account.

`admitted` lists every op the scan found admissible under C3's `admitted_on`
predicate -- the audit's own on-the-face computation, not this guard's -- keyed by
`op`, `max_observed_ms`, `occupancy_secs` so this guard can re-run the SAME imported
predicate (AC10) rather than trusting a `reasons` column the audit might compute
with a stale copy of the rule. This guard is one of `admitted_on`'s three named
consumers (`op_budget_suspension.admitted_on`'s own docstring); a change to the rule
changes this test's verdict along with C1's scan and C6's amended ratchet test.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List, Optional

import pytest
import yaml

from coordinator_core import ipc, op_budget_suspension, publish_lane
from coordinator_core.op_budget_suspension import admitted_on
from coordinator_core.telemetry.op_latency import sink_generations


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
    "session.boot_sweep",
    # hooks.cater_subagent_start — REINSTATED 65bbe1323, pruned here 2026-08-22.
    # fleet.archive_completed_handoffs — row REMOVED by PM ruling 2026-08-26 and
    # pruned here in the SAME commit. Not an op earning its way back: the row was
    # keyed to an implementation deleted at 648f2e4eb, so it had stopped
    # suspending what was killed and started blocking the from-scratch rebuild
    # the kill ruling asked for. Rebuilt op measured 212.5ms CPU / 267.2ms wall
    # via its handler against the row's 26111.9ms. See the removal note in
    # op_budget_suspension.SUSPENDED_OPS for the full reasoning.
    #
    # --- the 200ms sweep, PM ruling 2026-08-27 (kill ledger K-103..K-115) -----
    # Fourteen rows added in one commit. These are GRAVESTONES, not suspensions:
    # each was drained from the four registration surfaces in the same commit, so
    # the row is a name with no code behind it, keeping the refusal loud instead
    # of degrading to METHOD_NOT_FOUND. The bar they died on is 200ms of PROCESS
    # time (PROCESS_BAR_MS), not this module's 2000ms wall-clock occupancy bar --
    # see test_every_entry_carries_its_measured_evidence, which now reads both
    # axes. Six carry wall-clock-only evidence and say so in their own rows.
    # --- the brightline kill of the one job, plan 2026-08-29-the-housekeeping-
    # cycle-stops-committing (C9). Same gravestone shape as the fourteen below:
    # module and all four registration entries deleted in the same commit that
    # added the row, so the refusal stays loud. Its successor housekeeping.cycle
    # is live and does the same job at 109.4ms.
    "handoff.housekeeping",
    "ceremony.post_commit_tail",
    "write_surface.emit_manifest",
    "deliverable.cascade_terminal",
    "fleet.prune_closed_bugs",
    "ceremony.commit",
    "eol.census",
    "eol.repair",
    "roadmap.serve",
    "handoff.reconcile_open",
    "handoff.archive_transition",
    "review_trail.write",
    "session.sweep_consumed_handoffs",
    "cartography.churn",
    "handoff.has_live_children",
    "handoff.reconcile_close_terminal",
    "merge_assemble.brief",
    "fleet.archive_completed_plans",
})


#: The 18 rows C3 re-read against `op_adjudication.adjudicate`, 2026-08-30
#: (plan `2026-08-29-a-zero-is-under-one-tick-not-unmeasured.md`). Fixed by
#: that chunk's own dispatch brief, not derived from the live roster: a row
#: added to SUSPENDED_OPS after C3 landed owes its own citation on its own
#: schedule and this list must not silently grow to demand one of it.
_C3_CITED_OPS = frozenset({
    "session.boot_sweep",
    "ceremony.post_commit_tail",
    "write_surface.emit_manifest",
    "deliverable.cascade_terminal",
    "fleet.prune_closed_bugs",
    "ceremony.commit",
    "eol.census",
    "eol.repair",
    "roadmap.serve",
    "handoff.reconcile_open",
    "handoff.archive_transition",
    "review_trail.write",
    "session.sweep_consumed_handoffs",
    "cartography.churn",
    "handoff.has_live_children",
    "handoff.reconcile_close_terminal",
    "merge_assemble.brief",
    "fleet.archive_completed_plans",
})

_C2_CONFIDENCE_LABELS = frozenset({"EXACT", "FLOOR", "SPAWNS-UNKNOWN", None})
_C2_VERDICTS = frozenset({"adjudicated", "unadjudicated", "no_rows_in_window"})


def test_c3_cited_ops_are_still_on_the_roster():
    """The 18 named by C3's dispatch brief must not have quietly vanished --
    a removal is real news (`test_reinstated_ops_are_pruned_from_the_floor`'s
    job to police on the ratified floor), but this fixed list is a second,
    independent check specific to the batch this plan's C3 chunk actually read.
    """
    live = set(op_budget_suspension.SUSPENDED_OPS)
    missing = _C3_CITED_OPS - live
    assert not missing, (
        f"C3 cited these ops against the adjudication query, and they are no "
        f"longer on the roster: {sorted(missing)}. If they earned their way "
        f"back or were deleted, that is real news belonging in its own commit "
        f"note, not a silent disappearance from this fixed list."
    )


def test_every_c3_cited_entry_carries_a_c2_citation():
    """Prime exit criterion (plan `2026-08-29-a-zero-is-under-one-tick-not-
    unmeasured.md`): every one of the 18 entries cites a figure produced by
    `op_adjudication`'s query, recorded with the op, the route, the explicit
    `t_start` bounds of its window, and its EXACT/FLOOR/SPAWNS-UNKNOWN
    confidence label with n -- never a bare number with no instrument behind
    it. This guard checks the SHAPE of that citation, not its numeric value
    (this file's own negative-spec: no test here pins a measured number).
    """
    for op in sorted(_C3_CITED_OPS):
        record = op_budget_suspension.SUSPENDED_OPS.get(op)
        assert isinstance(record, dict), f"{op}: not on the roster"
        citation = record.get("c2_citation")
        assert isinstance(citation, dict), (
            f"{op}: no c2_citation -- every C3-readjudicated row must cite a "
            f"figure produced by op_adjudication.adjudicate, not a hand-derived "
            f"number nobody can rerun"
        )
        assert "route" in citation, f"{op}: c2_citation missing route"
        assert "n" in citation and isinstance(citation["n"], int), (
            f"{op}: c2_citation missing an integer n"
        )
        assert citation.get("confidence") in _C2_CONFIDENCE_LABELS, (
            f"{op}: c2_citation confidence {citation.get('confidence')!r} is not "
            f"one of EXACT/FLOOR/SPAWNS-UNKNOWN/None"
        )
        assert citation.get("verdict") in _C2_VERDICTS, (
            f"{op}: c2_citation verdict {citation.get('verdict')!r} is not a "
            f"recognised op_adjudication verdict"
        )
        assert "t_start_min" in citation and "t_start_max" in citation, (
            f"{op}: c2_citation missing its window's t_start bounds"
        )
        if citation.get("verdict") == "no_rows_in_window":
            assert citation.get("n") == 0, (
                f"{op}: no_rows_in_window citation carries a nonzero n"
            )
        else:
            assert citation.get("n", 0) > 0, (
                f"{op}: citation claims rows exist but n is not positive"
            )


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
    """Amended 2026-08-23 (C4, staff-eng F2): a row admitted on occupancy alone
    cannot satisfy `max_ms > _RATCHET_BAR_MS` by definition -- it was never a max
    breach. Every row must breach on max OR carry an `admitted_on` occupancy figure
    at or above the ratified `OCCUPANCY_BAR_SECS`, read through the SAME imported
    predicate C1's scan and this guard's own audit check use (AC10) -- never a
    parallel re-derivation of the rule.
    """
    for op, record in op_budget_suspension.SUSPENDED_OPS.items():
        measured = record.get("measured")
        assert isinstance(measured, dict), f"{op}: no measured evidence"
        max_ms = measured.get("max_ms")
        assert isinstance(max_ms, (int, float)), f"{op}: max_ms is not a number"

        occupancy_secs = record.get("occupancy_secs", 0.0)
        assert isinstance(occupancy_secs, (int, float)), (
            f"{op}: occupancy_secs is not a number"
        )

        # Amended 2026-08-27: a THIRD admission axis. The 200ms sweep
        # (K-103..K-115) convicts on process time against PROCESS_BAR_MS, an
        # axis `admitted_on` cannot see -- it takes a wall-clock max and an
        # occupancy figure, and a row like write_surface.emit_manifest (1562.5ms
        # max, well under the 2000ms wall bar) is a real breach on the axis that
        # actually governs. Reading only the two old axes would have called
        # every row in that sweep "a dial in disguise" and demanded its
        # reinstatement. The unit is carried per-row rather than inferred, so
        # this check reads what the row says it was judged on.
        unit = str(measured.get("unit") or "")
        p50_ms = measured.get("p50_ms")
        if unit.startswith("process_ms"):
            assert isinstance(p50_ms, (int, float)), f"{op}: p50_ms is not a number"
            assert float(p50_ms) > op_budget_suspension.PROCESS_BAR_MS, (
                f"{op} carries unit={unit!r} but its p50 ({p50_ms}ms process) does "
                f"not breach the {op_budget_suspension.PROCESS_BAR_MS}ms process "
                f"bar. A row judged on process time must breach on process time."
            )
            continue
        if unit == "WALL_CLOCK":
            # Convicted without a process-time measurement. The gap is real and
            # is NAMED in the row's own note rather than papered over; what this
            # guard can still enforce is that the wall figure is at least a
            # breach of the bar it was read against.
            assert isinstance(p50_ms, (int, float)), f"{op}: p50_ms is not a number"
            assert float(p50_ms) > op_budget_suspension.PROCESS_BAR_MS, (
                f"{op} is convicted on wall clock alone and does not even breach "
                f"the {op_budget_suspension.PROCESS_BAR_MS}ms bar on that looser "
                f"unit. Measure it with benchmarks/process_time before keeping it."
            )
            continue

        reasons = admitted_on(float(max_ms), float(occupancy_secs))
        assert reasons, (
            f"{op} is suspended but its recorded max ({max_ms}ms) breaches neither "
            f"the {_RATCHET_BAR_MS}ms bar nor the ratified occupancy threshold. "
            f"Either the evidence is stale or the op should be reinstated — a "
            f"suspension with no breach behind it is a dial in disguise."
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


#: The disposition every refusal ends on. Amended 2026-08-23 on PM ruling
#: ("kill means kill forever"): the message used to end "Prove it under 2s
#: without the warm engine to bring it back", which told every reader that
#: tuning the dead implementation was the route back. It is not, and never
#: was — see `op_budget_suspension`'s module docstring.
#:
#: Split 2026-08-27 into an invariant head and a conditional clause. The head
#: holds for every row. The clause is the right disposition only for a row
#: whose job is genuinely unhomed; on a row whose job was rehomed BY RULING it
#: sends the reader off to build what already exists. `review_trail.write` is
#: the measured case — DR-372 rehomed the job to the dispatched-agent sidecar
#: receipt, and two DoE sessions read the refusal as a fleet-wide capability
#: gap. Both directions are pinned below so the clause cannot be dropped from a
#: row that still owes it, nor kept on a row that does not.
_REFUSAL_TAIL_HEAD = "Killed, not suspended -- the old implementation does not come back."
_REFUSAL_TAIL_PLAN_CLAUSE = " If the job is still needed, plan a new one under 200ms."
_REFUSAL_TAIL = _REFUSAL_TAIL_HEAD + _REFUSAL_TAIL_PLAN_CLAUSE


def test_refusal_never_offers_a_way_around_it():
    """Register guard: the message states the fact, optionally the caller's
    sanctioned fallback, and the disposition — never a way around the refusal.

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
        assert _REFUSAL_TAIL_HEAD.lower() in lowered, (
            f"{op}'s refusal omits the disposition, which is the whole message: "
            f"the code is dead and only a new implementation replaces it"
        )
        record = op_budget_suspension.SUSPENDED_OPS[op]
        successor_live = bool(
            isinstance(record, dict) and record.get("successor_live")
        )
        if successor_live:
            assert _REFUSAL_TAIL_PLAN_CLAUSE.lower() not in lowered, (
                f"{op}'s job was rehomed by ruling, so its refusal must not send "
                f"the reader off to plan a replacement that already exists: {message!r}"
            )
            assert record.get("fallback"), (
                f"{op} declares successor_live with no fallback naming the live "
                f"mechanism — the reader is then told the op is dead and given "
                f"nowhere to go, which is the defect the flag exists to fix"
            )
        else:
            assert _REFUSAL_TAIL_PLAN_CLAUSE.lower() in lowered, (
                f"{op}'s job is unhomed and its refusal omits the plan-a-new-one "
                f"disposition: {message!r}"
            )




def test_suspension_bar_ratchet_direction_not_current_value():
    """AC8: the invariant is DIRECTION, not the pinned literal.

    `test_bar_is_never_raised` compares against `_RATCHET_BAR_MS`, which pins
    today's value -- a bar lowered to, say, 1000.0 still passes that test, and
    that is correct: the ratchet only forbids the RAISE direction. This test
    makes that direction explicit and independent of any particular literal,
    so a future edit cannot satisfy the letter of the pinned-literal test while
    violating the actual rule it exists to enforce.
    """
    assert op_budget_suspension.SUSPENSION_BAR_MS <= 2000.0, (
        "SUSPENSION_BAR_MS exceeds the ceiling every prior measurement was taken "
        "against. Lower it or leave it -- it may never move up."
    )


# AC8 vectors -- each below is a LIVE attempt against the running mechanism, not a
# reading of the source. A non-lane op (`queue.close`) is used throughout: it is
# suspended, and it is NOT `ceremony.scoped_git_commit`, so any of these vectors
# succeeding here would prove a general escape hatch rather than the one deliberate,
# narrowly-scoped carve-out (DR-350) that this module's own docstring names.
_NON_LANE_SUSPENDED_OP = "session.boot_sweep"
assert _NON_LANE_SUSPENDED_OP in op_budget_suspension.SUSPENDED_OPS
assert _NON_LANE_SUSPENDED_OP not in publish_lane.PUBLISH_LANE_OPS


def test_env_var_cannot_lift_suspension(monkeypatch):
    """Vector 1: environment variable. Try the sanctioned lane's own env signal,
    plus plausible-looking override names, against an op the lane does not name.
    """
    op = _NON_LANE_SUSPENDED_OP
    for env_name, env_value in (
        (publish_lane.PUBLISH_LANE_ENV, "1"),
        ("COORDINATOR_SUSPENSION_BAR_MS", "999999"),
        ("COORDINATOR_OP_SUSPENSION_OVERRIDE", "1"),
        ("COORDINATOR_DISABLE_OP_SUSPENSION", "1"),
        (f"{op.upper().replace('.', '_')}_FORCE", "1"),
    ):
        monkeypatch.setenv(env_name, env_value)

        ipc.allow_unstamped_dispatch()
        response = asyncio.run(ipc.dispatch_message({
            "jsonrpc": "2.0",
            "id": 1,
            "method": op,
            "_origin_worktree": ".",
            "params": {},
        }))
        error = response.get("error")
        assert error is not None and error["code"] == ipc.OP_SUSPENDED_ERROR, (
            f"env var {env_name}={env_value!r} lifted the suspension on {op} at dispatch"
        )

        with pytest.raises(op_budget_suspension.OpSuspendedError):
            ipc.get_op_handler(op)

        assert op_budget_suspension.SUSPENSION_BAR_MS == 2000.0, (
            f"env var {env_name}={env_value!r} changed SUSPENSION_BAR_MS"
        )

        monkeypatch.delenv(env_name, raising=False)


def test_op_param_cannot_lift_suspension():
    """Vector 2: op parameter. A caller cannot talk its way past the refusal by
    shaping `params` -- the refusal fires before the handler (and its params) are
    ever reached.
    """
    op = _NON_LANE_SUSPENDED_OP
    for params in (
        {"force": True},
        {"bypass_suspension": True},
        {"override": True},
        {"publish_lane": True},
        {"_publish_lane": True},
        {"suspension_bar_ms": 999999},
    ):
        ipc.allow_unstamped_dispatch()
        response = asyncio.run(ipc.dispatch_message({
            "jsonrpc": "2.0",
            "id": 1,
            "method": op,
            "_origin_worktree": ".",
            "params": params,
        }))
        error = response.get("error")
        assert error is not None and error["code"] == ipc.OP_SUSPENDED_ERROR, (
            f"params {params!r} lifted the suspension on {op} at dispatch"
        )


def test_envelope_field_cannot_lift_suspension():
    """Vector 3: envelope field. `_publish_lane` is a REAL envelope field with REAL
    effect (DR-350) -- but only for the ops PUBLISH_LANE_OPS names. Stamping it on
    an envelope for an op outside that closed list must not lift anything, at
    either door.
    """
    op = _NON_LANE_SUSPENDED_OP
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": op,
        "_origin_worktree": ".",
        "_publish_lane": True,
        "params": {},
    }

    ipc.allow_unstamped_dispatch()
    response = asyncio.run(ipc.dispatch_message(msg))
    error = response.get("error")
    assert error is not None and error["code"] == ipc.OP_SUSPENDED_ERROR, (
        f"envelope field {publish_lane.PUBLISH_LANE_FIELD!r} lifted the suspension "
        f"on {op} at dispatch"
    )

    with pytest.raises(op_budget_suspension.OpSuspendedError):
        ipc.get_op_handler(op, msg)


def test_publish_lane_field_is_scoped_to_its_closed_list_only():
    """The one REAL lever (DR-350) proven not to generalise.

    Stamping the lane field/env DOES lift the refusal for the one op the lane
    names -- proving the field is live, not inert -- and then proving the exact
    same signal does nothing for a sibling op one line above it in the same
    roster. A vector that "does nothing at all" would be a weaker guard than one
    that is proven live and then proven scoped.
    """
    lane_op = "ceremony.scoped_git_commit"
    assert lane_op in publish_lane.PUBLISH_LANE_OPS

    # The lane field lifts the dispatch-time refusal for the lane op...
    budget = publish_lane.budget_for(lane_op, {publish_lane.PUBLISH_LANE_FIELD: True})
    assert budget == publish_lane.PUBLISH_LANE_BUDGET_SECS

    # ...and does nothing for a non-lane op carrying the identical signal.
    budget = publish_lane.budget_for(
        _NON_LANE_SUSPENDED_OP, {publish_lane.PUBLISH_LANE_FIELD: True}
    )
    assert budget is None


def test_rows_without_a_sanctioned_fallback_name_none():
    """The empty slot is the correct answer for most of this table, and inventing
    a plausible-sounding fallback is the same improvisation the field prevents.

    Only ops with a real caller-drivable mechanism carry one. Every other row's
    refusal is unchanged in shape: fact, then the disposition.
    """
    for op, record in op_budget_suspension.SUSPENDED_OPS.items():
        if record.get("fallback"):
            continue
        message = op_budget_suspension.refusal_message(op)
        # The figure-and-bar clause is rendered in the unit the row was judged
        # in (op_budget_suspension._bar_clause), so the prefix is "is off: "
        # plus one of three shapes rather than a hardcoded "max ". Pinning the
        # literal "max " asserted the OLD wall-clock framing and would fail
        # every process-time row in the 2026-08-27 sweep -- which is the
        # framing those rows exist to correct, not a regression.
        assert message.startswith(f"{op} is off: "), message
        assert any(
            marker in message
            for marker in ("ms process time against", "WALL CLOCK against", "against a ")
        ), message
        assert message.endswith(_REFUSAL_TAIL), message


def test_the_refusal_renders_how_the_number_arose():
    """A number without its instrument gets acted on as the other instrument.

    This assertion replaced one pinning the literal `"is off: measured max "`.
    That wording was the defect, not the contract: `session.boot_sweep`'s
    max_ms is `ipc.DISPATCH_TIMEOUT_SECS` -- the point the dispatcher gave up,
    recorded honestly in the row's own `note` ("8/8 ended in caller_timeout at
    30s") and then dropped by the message that called it "measured". Two EMs
    read it as a measured 15x overshoot and a cross-repo plan sized a rewrite
    against it. Calling a ceiling a measurement is the thing being prevented.
    """
    for op, record in op_budget_suspension.SUSPENDED_OPS.items():
        note = record.get("note")
        message = op_budget_suspension.refusal_message(op)
        assert "measured max" not in message, message
        if note:
            assert note.strip() in message, (
                f"{op}'s refusal drops its own note, which is the only place "
                f"the reader learns whether the number is a duration or a "
                f"ceiling: {message}"
            )


# --- § Roster COMPLETENESS (C4, AC11) ---------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Fixed by C2/C5's declared `writes:` scope in
#: `docs/plans/2026-08-23-the-roster-stops-being-hand-curated.md` -- the "most
#: recent committed audit record" this guard reads. A single fixed path, not a
#: glob over `state/audits/`, because the plan names exactly one file and a glob
#: would silently start matching an unrelated audit the moment one is added.
_AUDIT_PATH = (
    _REPO_ROOT
    / "state"
    / "audits"
    / "2026-08-23-the-op-table-against-both-admission-criteria.md"
)

#: EM-set at 10% (a RATIO of the audit's stamped total, not an absolute byte
#: count -- see the `growth` computation below), sourced from the audit
#: record's own item-5 candidate: `state/audits/2026-08-23-the-op-table-
#: against-both-admission-criteria.md`'s six-item ratification set named this
#: exact number with its cost stated. The PM ratification that would have set
#: this value was C6, and C6 was dispositioned `wont_do` in
#: `docs/plans/2026-08-23-the-roster-stops-being-hand-curated.md`'s 2026-08-23
#: delete-and-rebuild ruling -- the suspension lane C6 was going to land was
#: retired along with it, so waiting on that ratification means waiting on a
#: chunk that will never run. AC11 requires the freshness half to have real
#: teeth independent of the content half; an `inf` threshold left the
#: byte-growth comparison permanently inert, which is a gap now, not a
#: placeholder. At the corpus's measured growth rate (~6.8MB/day against a
#: ~102.5MB total) 10% fires roughly every 1.5 days of activity -- loose
#: enough not to flap on every cadence-tier run, tight enough that a stale
#: audit cannot survive a rotation cycle unnoticed. Still a ratchet: lowerable
#: once someone has better evidence, never raised back toward `math.inf`.
_AUDIT_STALENESS_BYTE_GROWTH_RATIO: float = 0.10


def _extract_admission_audit_block(markdown_text: str) -> Optional[dict]:
    """Pull the ```yaml admission-audit fenced block out of the audit markdown.

    Returns `None` if no such block is present -- an audit document that is
    prose-only (not yet amended to carry the machine-readable contract this
    module docstring states) is treated the same as a missing document: this
    guard SKIPs rather than failing on a document shape it cannot parse.
    """
    marker = "```yaml admission-audit"
    start = markdown_text.find(marker)
    if start == -1:
        return None
    body_start = start + len(marker)
    end = markdown_text.find("```", body_start)
    if end == -1:
        return None
    block_text = markdown_text[body_start:end]
    try:
        parsed = yaml.safe_load(block_text)
    except yaml.YAMLError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _load_audit() -> Optional[dict]:
    if not _AUDIT_PATH.is_file():
        return None
    text = _AUDIT_PATH.read_text(encoding="utf-8", errors="replace")
    return _extract_admission_audit_block(text)


def _live_sink_generations() -> List[Path]:
    """Existing generation files only -- `sink_generations` never raises and
    returns `[]` if the git common dir cannot be resolved (its own docstring)."""
    return [p for p in sink_generations(_REPO_ROOT) if p.is_file()]


@pytest.mark.designed_red
def test_roster_matches_most_recent_audit_content():
    """AC11 half 1 (content). Every op the audit found admissible (either axis,
    via the SAME imported `admitted_on` predicate C1's scan uses -- AC10) must be
    on the roster, unless the audit's own row carries a non-`admissible`
    `disposition` naming a reason (`spared`, `stale_measurement`, or `dead`).

    Artifacts to artifacts, never a timing: this reads the committed audit
    document and the in-tree `SUSPENDED_OPS` table, nothing live. SKIPs (never a
    silent pass) when the audit does not exist yet or does not carry the
    machine-readable block -- see module docstring's stated contract; this plan's
    own sequencing note lifts this guard ahead of C2/C5, which produce that
    document.
    """
    audit = _load_audit()
    if audit is None:
        pytest.skip(
            f"no admission-audit block found in {_AUDIT_PATH} (file missing, or "
            f"present but still prose-only) -- guard SKIPs per the three-state "
            f"norm rather than failing on a document shape it cannot parse"
        )

    admitted_rows = audit.get("admitted")
    assert isinstance(admitted_rows, list), (
        "admission-audit block is missing its 'admitted' list"
    )

    live_roster = set(op_budget_suspension.SUSPENDED_OPS)
    missing = []
    for row in admitted_rows:
        if not isinstance(row, dict):
            continue
        op = row.get("op")
        max_ms = float(row.get("max_observed_ms") or 0.0)
        occupancy_secs = float(row.get("occupancy_secs") or 0.0)

        if not admitted_on(max_ms, occupancy_secs):
            # The audit's own row claims admission but the CURRENT predicate
            # (which may have moved since the audit was written) disagrees --
            # not this test's problem; a stale admission claim is a freshness
            # concern, handled below, not a content one.
            continue

        if op in live_roster:
            continue
        if row.get("disposition") != "admissible":
            # spared / stale_measurement / dead -- a reasoned non-admission,
            # never this guard's problem to enforce.
            continue
        missing.append(op)

    assert not missing, (
        f"Ops the audit found admissible are neither on the roster nor carrying "
        f"a non-admissible disposition: {sorted(missing)}. Add them to "
        f"SUSPENDED_OPS with their measured evidence, or mark the audit row's "
        f"disposition 'spared'/'stale_measurement'/'dead' with a reason."
    )


@pytest.mark.cadence
def test_audit_is_not_stale_against_the_live_sink():
    """AC11 half 2 (freshness). Stamp-against-corpus, never stamp-against-date:
    a date can be bumped by anyone, a byte-size/generation-set comparison cannot.

    This assertion exists independently of the content check above (AC11's own
    text: "the freshness half must not be dischargeable by the content half
    alone") -- a guard that only checks content is green forever once nobody
    re-runs the audit, which reproduces this plan's own § Problem one artifact
    to the left.

    `cadence`-marked, not fast-tier: a byte-growth check firing on every fast
    run is the wrong cadence for a monthly-shaped fact (task body).

    SKIPs, never fails, when either artifact this comparison needs is absent:
    the audit document (not yet produced, same as the content test above) or the
    live sink (a fresh clone, a peer's box -- `.git/coordinator-sessions/logs/`
    is not a committed path). Both are the three-state norm `op_census/timing.py`
    already applies to missing telemetry.
    """
    audit = _load_audit()
    if audit is None:
        pytest.skip(
            f"no admission-audit block found in {_AUDIT_PATH} (file missing, or "
            f"present but still prose-only) -- nothing to check freshness against"
        )

    stamp = audit.get("stamp")
    assert isinstance(stamp, dict), "admission-audit block is missing its 'stamp'"

    stamped_paths = stamp.get("generation_paths")
    stamped_sizes = stamp.get("generation_byte_sizes")
    assert isinstance(stamped_paths, list), "stamp.generation_paths missing"
    assert isinstance(stamped_sizes, list), "stamp.generation_byte_sizes missing"

    live_generations = _live_sink_generations()
    if not live_generations:
        pytest.skip(
            "no live op-latency sink on this box -- fresh clone or peer box, "
            ".git/coordinator-sessions/logs/ is not a committed path"
        )

    live_paths = [str(p) for p in live_generations]
    live_sizes = []
    for p in live_generations:
        try:
            live_sizes.append(p.stat().st_size)
        except OSError:
            live_sizes.append(0)

    if set(live_paths) != set(stamped_paths):
        pytest.fail(
            f"audit's generation set does not match the live sink -- a rotation "
            f"happened since the audit was written (stamped {stamped_paths!r}, "
            f"live {live_paths!r}). Re-run the scan."
        )

    stamped_total = sum(int(s) for s in stamped_sizes)
    live_total = sum(live_sizes)
    growth_ratio = (
        (live_total - stamped_total) / stamped_total if stamped_total else 0.0
    )
    assert growth_ratio <= _AUDIT_STALENESS_BYTE_GROWTH_RATIO, (
        f"live sink has grown {growth_ratio:.1%} past the audit's stamped total "
        f"({stamped_total} -> {live_total}), past the ratified "
        f"{_AUDIT_STALENESS_BYTE_GROWTH_RATIO:.0%} byte-growth threshold. "
        f"Re-run the scan."
    )
