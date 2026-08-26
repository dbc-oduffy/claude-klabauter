"""coordinator_core.op_budget_suspension — the ops that are OFF for blowing the 2s bar.

Purpose: stop an over-budget op from firing at all, rather than granting it a
larger timeout. A suspended op is refused at dispatch, before its owning module
is imported, so a breach costs the box nothing — not the work, not the import.

PM ruling, 2026-08-21, verbatim: *"everything taking over 2s max should be turned
off. turning them back on, each of them needs to earn it. nothing is worth >2s,
especially now we have a warm engine. so, off now, to save the box, and stop this
bullshit where they get paper cuts."*

The bar is MAX, not p50 or p95. An op whose median is fast and whose worst case is
56 seconds has still occupied the box for 56 seconds, and at this machine's load
norm (~50 concurrent sessions) that is 50 peers held behind one invocation. A
distribution argument is exactly the "paper cuts" move the ruling names: every
individual breach is defensible in isolation and the box still dies of them.

Why suspension and not a lowered timeout: a timeout that fires still paid for the
work up to the bound, and the op comes back next invocation to pay again. A dial
lowered to 2s converts a 30s breach into fifteen 2s breaches. Off is the only
disposition that returns the time.

THERE IS NO REINSTATEMENT LANE, AND A ROW HERE IS DEAD, NOT PAUSED.
    PM ruling, 2026-08-23, verbatim: *"kill means kill forever... rebuild from
    scratch should be the norm."*

    ONE PM CARVE-OUT EXISTS, AND IT IS NOT A LANE. `review_trail.write` was
    reinstated by PM ruling on 2026-08-23 -- verbatim: *"ok fine we can
    reinstate this thing"* -- leaving this table with its implementation
    intact. That is NOT an op earning its way back, which remains impossible.
    It is the correction of a row that should never have been written.

    Its suspension figures (p50 3986.3ms, max 16558.8ms, "96% breach rate over
    108 invocations") are WALL CLOCK off the op-latency sink, and `CLAUDE.md`
    § The brightline refuses wall clock as a cost measurement. Measured on
    process time by `benchmarks/process_time.batched_process_time_ms`
    (claude-klabauter-24, 2026-08-23, commits 060230992 / 5ffce81fc / bcba631b2):
    212.5ms cold end-to-end concrete, 296.9ms symbolic, 46.9ms warm marginal --
    under the 500ms brightline on every sample taken. It never met the kill bar
    on the axis that governs, and 46% of its cold cost is its own module import
    against 16% git, so it is not even the shape the bar targets.

    Read this before writing a row off that sink: a suspension justified by
    wall clock is not evidence, and the answer to one is to measure process
    time, not to defend the row. The cost of this one standing was a real audit
    surface -- no review was recordable fleet-wide while it held
    (`state/bug-backlog/2026-08-23-review-trail-write-is-suspended-in-its-own-right-so-no-review-is-recordable-fleet-wide.yaml`).

    This block previously described how a suspended op earns its way back by
    proving the SAME implementation runs under 2s. That reading is retired, and
    the reason is measured, not stylistic: it minted eighteen
    `state/handoffs/2026-08-21-earn-*-back-under-2s-cold.md` batons whose
    success condition was, by construction, a refactor of the code that had
    already failed the bar. Yield, verbatim from one of them on 2026-08-23:
    "7.9s wall / 1513ms process / 31 procs after four spawn cuts". Four rounds
    of L-tier effort, still 3x over the only bar that governs.

    What may follow a dead row is a REQUIREMENT, never the code. Exactly one
    question is asked of it -- does anything still need this job done?

        No  -> a gravestone. The row stays, the code is deleted, the matter is
               closed forever. `artifact.emit` (CUT in full per PM, 2026-08-23)
               is the worked example.
        Yes -> a NEW plan, sized against the requirement in the PM's words,
               spiked under DR-344 SS1-3, written from first principles. The
               deleted implementation is not a starting point, not a reference,
               and not a thing to be made incrementally cheaper. `git` retains
               it; doctrine does not.

    A row leaves this table when a NEW implementation clears 500ms, never when
    the old one is tuned.

    `SUSPENSION_BAR_MS` (2000ms) IS AN ADMISSION THRESHOLD, NOT A TARGET.
    It answers "which ops were box-occupying enough to switch off on the spot",
    and nothing else. It is 4x the brightline, so an op scored against it reads
    as nearly-there while sitting 3x over the number that governs -- which is
    exactly how a 20ms saving came to be reported as progress. Nothing is
    designed, sized, accepted, or reported against 2000ms. DR-344 SS6: one bar,
    and it is 500ms.

    The measurement discipline below still binds every NEW implementation, and
    is retained for that reason -- it was written at the seam where the
    temptation to cite a friendly number is highest.

    ON BOTH PLATFORMS, AND IT IS ONE BAR, NOT TWO. A case built on macOS numbers
    alone does not discharge it, and neither does one built on Windows numbers
    alone. Process creation -- the dominant cost of every op in this table -- is
    far more expensive on Windows than on macOS, measured at roughly 50x for the
    same work: `review_trail.write` reads p50 74.1ms / max 1484.5ms over n=938
    on one box's macOS sink and p50 3986.3ms / max 16558.8ms over n=108 in the
    Windows measurement this table's own row carries. Both figures are correct.
    A single-platform case therefore either under-reports the real cost by that
    factor or condemns an op that is fine where it actually runs, and a reader
    cannot tell which from the number alone.

    What this is NOT: a per-platform threshold, a platform column in the roster,
    or a suspension that lifts on one OS and holds on another. PM ruling,
    2026-08-22: the bans stay until the ops are rebuilt to be performant on both
    platforms, and an OS-aware ban mechanism was rejected outright -- it is one
    step from "this is fine on macOS, so un-ban it here", which converts a
    fleet-wide correctness bar into a local opt-out and leaves the op slow on
    Windows with nobody feeling it. The single uniform number is the feature.

    SPAWN COUNT IS THE LOAD-INVARIANT FIGURE, AND IT IS REQUIRED.
    A wall-clock number taken under load is mostly a measurement of this session's
    peers, not of the op. Measured on this box, 2026-08-21, same three git reads
    taken twice: in-process 31ms under load vs 30ms on a quiet box — flat; three
    subprocess spawns 3,188ms under load vs 158ms quiet — a 20x swing. A single
    `git --version` ranged 37ms to 1,394ms with ~0ms CPU either way. The cost is
    process creation, not the work.

    So a reinstatement case that reports only wall time is not reproducible: the
    same code passes at 3am and fails at noon. Every case MUST report spawn count
    and process time alongside any wall figure, and the spawn count is the number
    that decides it. This restates CLAUDE.md § brightline ("process time and spawn
    count, never wall clock") at the seam where the temptation to ignore it is
    highest — the moment someone wants their op back.

    AND REPORT n WITH QUANTILES, NEVER A SINGLE SAMPLE.
    Measured here at n=30: `git --version` — the most trivial control that exists,
    doing no work whatsoever — runs 15.3ms at min, 17.5ms at p50, and 279.3ms at
    p99. A ~20x spread on nothing. One sample on this box measures the peer fleet,
    not the code.

    That is not hypothetical: five figures in the campaign that produced this table
    failed this way and were withdrawn. Three rules survive them, and they are the
    contract this docstring owes a reader:

        Re-run it — a single sample on a box with real spread is not a result.
        Re-open it — having once seen a number is not evidence, and a scratch copy
            of one is not a source. This applies hardest to a figure relayed from
            somebody else, which arrives looking checked precisely because it cites.
        Prefer a control — holding conditions constant beats measuring carefully
            under conditions you do not control.

    The campaign narrative behind those rules — which five figures, how each failed,
    which were caught by their own author and which by a reader — is a dated record,
    not a contract, and lives in
    `state/2026-08-21-ops-turned-off-for-the-2s-max-bar.md`. Those numbers go stale;
    the three rules do not.

    So: p50 and p90 over a real n, or the case is not made. And prefer a CONTROL to
    a sample wherever one is available — the strongest evidence this campaign
    produced was 160s-through-the-op against 8.9s-through-plain-git-commit, same
    paths, same branch, same minute. Holding conditions constant beats measuring
    more carefully under conditions you do not control.

    The `measured` numbers in this table are wall-clock occupancy from the
    op-latency sink, and they are stated as such. That is the correct measure for
    what this table is FOR — an op holding the box for 30s holds it for 30s
    whatever the reason — and it is the wrong measure for deciding whether a
    rebuild succeeded. Do not reuse these numbers as a reinstatement baseline
    without converting to spawn count first.

Negative-spec:
    - This table SHRINKS, never grows by tuning. An op leaves it by being made
      fast or by being deleted, never by being re-measured on a quiet box.
    - No environment variable, parameter, or envelope field lifts a suspension.
      There is deliberately no argument this module accepts that changes its
      answer — the knob that would be the escape hatch does not exist to be found.
    - BOTH invocation doors are guarded, not just the front one. `ipc`'s JSON-RPC
      dispatch refuses with `-32006`; `ipc.get_op_handler` raises
      `OpSuspendedError`. The second is not redundant: resolving a handler by key
      and awaiting it directly ("path 3", see `warm/entry_seam.py`) is a dozen-site
      production pattern here, and guarding only dispatch left the roster's two
      most expensive ops fully reachable — `safe_commit_offer.py :: _commit_group`
      resolves `ceremony.scoped_git_commit`, `tail_ops.py` resolves
      `review_trail.write`. A guard on the door people do not use is not a guard.
    - Refusal happens BEFORE registry lookup and therefore before the lazy import
      of the op's owning module. Refusing after the import would still pay the
      import cost, which for several of these ops is the cheap part of a very
      expensive call, but is not nothing at 50 concurrent sessions.
    - The refusal message never names a bypass, and never offers "raise the
      timeout" as the alternative. There is no shape of this message in which
      that text is correct. Conforms to docs/wiki/guard-messaging.md § Register.

      A SANCTIONED FALLBACK IS NOT A BYPASS, and the distinction is a PM ruling
      of 2026-08-21, not an EM reading of this bullet: *"plain git commit is and
      has to be the sanctioned fallback for now."* A bypass would defeat the
      suspension — it would get the op's work done at the op's cost, which is
      the box damage this table exists to stop. Plain `git commit` does not: it
      is a different, cheaper mechanism that every EM in this repo already uses,
      and the `prepare-commit-msg` hook attributes it identically (verified by
      control, 2026-08-21 — six commits, plain git, zero op invocations, all
      carrying `Deliverable-Id`). Naming it costs the ruling nothing.

      "FOR NOW" IS PART OF THE RULING, so it is part of this bullet. The
      fallback stands while the suspension does. If `ceremony.scoped_git_commit`
      earns its way back, the `fallback` row leaves with it — this is not a
      standing licence to hand-commit around a live op, and a reader who finds
      this text after reinstatement is reading a stale carve-out.

      The bar for adding a `fallback` to any other row: a mechanism that already
      exists, that a caller can drive without the op, and that preserves what the
      op guaranteed. Absent all three, the row carries none. An empty slot is the
      correct answer far more often than a plausible-sounding one — the failure
      this field was built for was an agent inventing a disposition, and an EM
      inventing one here is the same error one layer up.
    - `measured` is EVIDENCE, not a threshold. Nothing reads these numbers to
      make a decision; they exist so the refused caller learns why without going
      to look, and so a reinstatement case has a baseline it must beat.
    - Membership is by exact op name. No prefix matching: unlike the ceremony
      budget, this is not a class of ops that should be born inside a rule, it is
      a finite list of specific defects, and every entry is meant to leave.

Spec backlink: docs/decisions/DR-349-one-budget-governs-every-constructed-op.md
               docs/problems/2026-08-21-the-over-budget-timeout-hitlist.md
               state/sizings/2026-08-21-lower-the-global-op-budget-to-2s-and-ado.yaml
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

__all__ = [
    "SUSPENSION_BAR_MS",
    "OCCUPANCY_BAR_SECS",
    "SUSPENDED_OPS",
    "OpSuspendedError",
    "is_suspended",
    "suspension_record",
    "refusal_message",
    "admitted_on",
]


class OpSuspendedError(RuntimeError):
    """Raised when a suspended op is resolved for IN-PROCESS invocation.

    The JSON-RPC dispatch path refuses a suspended op with `-32006` and never
    reaches this. This exists for the OTHER way an op gets invoked in this
    codebase: `ipc.get_op_handler(name)` returning a callable the caller then
    awaits directly — "path 3" re-entry, formalised in `warm/entry_seam.py` and
    used from a dozen production sites. Refusing only at dispatch left the two
    most expensive ops in the roster reachable at full cost through that door
    (`safe_commit_offer.py :: _commit_group` resolves `ceremony.scoped_git_commit`;
    `tail_ops.py` resolves `review_trail.write`).

    Why an exception rather than returning None: `get_op_handler`'s contract is
    that None means "genuinely unregistered", and callers branch on that as a
    not-found condition. A suspended op is registered and would work — returning
    None would make it indistinguishable from a missing op and let a caller
    degrade past it silently, which is the opposite of the ruling's intent that
    the failure be visible. Loud is the point.
    """


# The PM's bar, in milliseconds, measured as MAX end-to-end per invocation.
# This number may be LOWERED, never raised — DR-344's brightline is 500ms and this
# is already four times more generous than the target it serves. The ratchet test
# `coordinator_core/tests/test_op_suspension_ratchet.py` fails on any edit lifting it.
SUSPENSION_BAR_MS = 2000.0


# Measured 2026-08-21 by `op_census.breaches` (top_n=null) over the current
# op-latency generation, corroborated by a direct full-generation read. Every row
# here blew SUSPENSION_BAR_MS on MAX. Ops that breached DR-344's 500ms brightline
# but stayed under 2s on max are deliberately ABSENT — they are defects, but they
# are not box-occupying defects, and this table is the box's defence, not the
# brightline's enforcement.
#
# The four `test.*` fixtures that breach by construction are also absent, and not
# by carve-out: their max latencies (66-110ms) simply fit. A rule that needed an
# exception for its own test fixtures would be the wrong rule.
SUSPENDED_OPS: Dict[str, Dict[str, object]] = {
    "session.boot_sweep": {
        "measured": {"max_ms": 30016.6, "p50_ms": 30010.8, "n": 8},
        "note": "8/8 ended in caller_timeout at 30s.",
        "spinoff": None,
    },
    # fleet.archive_completed_handoffs — REMOVED 2026-08-26 by PM ruling, and
    # pruned from test_op_suspension_ratchet._RATIFIED_SUSPENSIONS in this same
    # commit (that direction was got wrong once before; see that frozenset's
    # own comment).
    #
    # NOT an op earning its way back — that lane does not exist and this is not
    # it. The row recorded `max_ms: 26111.9, n: 1` and its refusal text read
    # "Killed, not suspended — the old implementation does not come back. If
    # the job is still needed, plan a new one under 500ms." That is exactly
    # what happened: `ops/fleet/archive_handoffs.py` was DELETED at 648f2e4eb
    # and the op key re-registered by `ops/fleet/archive_terminal_handoffs.py`,
    # a from-scratch rebuild. The row was keyed to an op NAME whose
    # implementation no longer existed, so it had stopped suspending the thing
    # that was killed and started blocking the replacement the ruling asked
    # for. Same shape as the `review_trail.write` correction above, on firmer
    # ground: there the code was unchanged, here it was rebuilt as directed.
    #
    # Measured before removal, not after — the rebuilt op through its
    # registered handler, live corpus, cold interpreter included:
    # 212.5ms CPU / 267.2ms wall / 4 processes on dry_run, against the row's
    # 26111.9ms. Two orders of magnitude apart.
    # → docs/plans/2026-08-25-the-terminal-handoff-sweep-stops-being-an-op.md
    #   § AC-8, and state/audits/2026-08-25-the-handoff-archive-op-earns-its-
    #   way-back.md § AC-7 re-take 2026-08-26.
    #
    # NEGATIVE SPEC for anyone re-adding this key: a suspension row is keyed by
    # NAME, and a name survives the deletion of the code it named. Before
    # writing one, check that the implementation you measured is the
    # implementation the key resolves to today.
}


# The second admission axis (staff-eng F6 / DR-349 addendum): an op that never
# breaches SUSPENSION_BAR_MS on MAX can still hold the box for more cumulative
# box-seconds than the whole roster combined, and the max-only criterion is blind
# to it (C1's occupancy scan; state/audits/2026-08-23-the-op-table-against-both-
# admission-criteria.md). `admitted_on` below is the SIGNATURE this plan lifts
# ahead of the PM gate (C4's ratchet-completeness guard needs it to import NOW);
# the ratified value is a C2-recommended, PM-ratified absolute box-seconds figure
# and lands at C6 together with the admitted rows, in the same commit as the
# roster change (the plan's one-commit constraint).
#
# `math.inf` is the placeholder, not a guess at the ratified number: it makes the
# occupancy leg of `admitted_on` inert (nothing has infinite occupancy) rather
# than silently admitting or excluding rows on a number nobody has ratified yet.
# This is a ratchet exactly like `SUSPENSION_BAR_MS`: once C6 sets a finite value,
# it may only be LOWERED, never raised back toward `math.inf`.
OCCUPANCY_BAR_SECS: float = math.inf


def admitted_on(max_observed_ms: float, occupancy_secs: float) -> List[str]:
    """Which axis or axes admit an op onto the roster; `[]` means neither.

    One predicate, stated once (this plan's own doctrine backlink,
    `docs/wiki/computed-fact-in-prose-is-break-class.md`): emit a named field,
    not prose asserting the same fact. An op is admitted if its windowed max
    exceeds `SUSPENSION_BAR_MS`, or its box-seconds exceed `OCCUPANCY_BAR_SECS`,
    or both — the return value names WHICH, as `["max"]`, `["occupancy"]`, or
    `["max", "occupancy"]`, so a caller never re-derives the reason from the two
    constants by hand.

    IMPORTED, not cited, by three consumers (AC10) — C1's scan
    (`op_census.occupancy_scan`, which populates the candidate list's
    `admitted_on` field), and, in `tests/test_op_suspension_ratchet.py`, both
    `test_roster_matches_most_recent_audit_content` (C4's ratchet-completeness
    guard, checking the audit against the roster) and
    `test_every_entry_carries_its_measured_evidence` (C4): a change to the rule
    changes all three together. `OCCUPANCY_BAR_SECS` is a placeholder
    (`math.inf`) until C6 ratifies it, so every caller of this predicate today
    reads as max-only in practice — that is the correct behaviour of an unset
    ratchet, not a bug to work around here.

    *max_observed_ms* and *occupancy_secs* are the windowed figures from C1's
    `op_census.occupancy_scan.OpOccupancy` (`max_observed_ms`, `occupancy_secs`)
    — this module does not import that dataclass, to keep the predicate usable
    from either direction without a circular import. `max_observed_ms` there is
    typed `Optional[float]` (`None` when an op has zero complete rows in the
    window); this predicate takes a plain `float` on purpose, so a caller MUST
    coerce `None` before calling (`acc.max_observed_ms or 0.0`, as the live C1
    call site does) — `None > SUSPENSION_BAR_MS` raises `TypeError` otherwise.
    The plain-float shape is deliberate, not an oversight: accepting `Optional`
    here would pin an import direction between this module and `op_census` that
    the circular-import avoidance above exists to prevent.
    """
    reasons: List[str] = []
    if max_observed_ms > SUSPENSION_BAR_MS:
        reasons.append("max")
    if occupancy_secs > OCCUPANCY_BAR_SECS:
        reasons.append("occupancy")
    return reasons


def is_suspended(method: object) -> bool:
    """True when *method* is an op that has been turned off for blowing the bar."""
    return isinstance(method, str) and method in SUSPENDED_OPS


def suspension_record(method: str) -> Optional[Dict[str, object]]:
    """The measured evidence behind *method*'s suspension, or None if it is live."""
    return SUSPENDED_OPS.get(method)


def refusal_message(method: str) -> str:
    """The caller-facing refusal for a suspended op.

    Register (docs/wiki/guard-messaging.md): one fact, once, then a terse
    imperative. The reinstatement bar — cold, not warm — is never a way around
    the refusal, because there is not one.

    TWO AUDIENCES, and the reinstatement bar only ever addressed the second.
    A refused caller is mid-workflow and cannot reinstate anything; telling it
    what a future candidate build must prove leaves it with nothing to do now.
    Measured consequence (2026-08-21): a dispatched execution workflow halted
    at its first commit gate and the commit agent, bound by its own definition
    to `ceremony.scoped_git_commit` and forbidden a raw `git commit`, invented
    the disposition it had not been given — "wait for the infrastructure to
    recover", advice naming an event this hand-curated table cannot emit.

    So a row MAY carry a `fallback`: the sanctioned path the caller takes right
    now. It is rendered before the reinstatement bar, because the caller reads
    in that order. Only `ceremony.scoped_git_commit` carries one — most of these
    ops have no equivalent a caller can drive by hand, and inventing one to fill
    the slot would be the same improvisation the field exists to prevent.
    """
    record = SUSPENDED_OPS.get(method)
    max_ms = 0.0
    fallback = ""
    note = ""
    if isinstance(record, dict):
        measured = record.get("measured")
        if isinstance(measured, dict):
            try:
                max_ms = float(measured.get("max_ms") or 0.0)
            except (TypeError, ValueError):
                max_ms = 0.0
        raw_fallback = record.get("fallback")
        if isinstance(raw_fallback, str):
            fallback = raw_fallback.strip()
        raw_note = record.get("note")
        if isinstance(raw_note, str):
            note = raw_note.strip()
    return (
        # A NUMBER TRAVELS WITHOUT ITS INSTRUMENT UNLESS THE MESSAGE CARRIES IT.
        # `session.boot_sweep`'s max_ms is 30016.6 and its note is "8/8 ended in
        # caller_timeout at 30s" -- the figure is `ipc.DISPATCH_TIMEOUT_SECS`,
        # the point where the dispatcher gave up, not a duration anything ran
        # for. Rendering it as "measured max 30016ms" and dropping the note read
        # to two EMs (claude-klabauter-em and doe-claude-em, 2026-08-26) as a
        # measured 15x overshoot of the bar, and a cross-repo plan sized a
        # from-scratch rewrite against it before either of us read the note that
        # was in the record all along. The record was honest; the message was
        # not. DoE's own corpus had already ruled this class three days earlier
        # -- state/lessons/2026-08-23-a-number-without-its-instrument-gets-acted
        # -on-as-the-other-instrument.md.
        #
        # A timeout-derived figure is a FLOOR on the op's cost and says nothing
        # about its real duration: the op could be barely over the bar or
        # hundreds of times over it. That distinction is exactly what a sizing
        # decision turns on, so it is rendered here rather than left for a
        # reader to go find.
        f"{method} is off: max {max_ms:.0f}ms against a "
        f"{SUSPENSION_BAR_MS:.0f}ms bar. "
        + (f"How that number arose: {note} " if note else "")
        + (f"{fallback} " if fallback else "")
        + "Killed, not suspended -- the old implementation does not come "
        "back. If the job is still needed, plan a new one under 500ms."
    )
