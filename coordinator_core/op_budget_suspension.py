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

§ c2_citation (added 2026-08-30, plan `2026-08-29-a-zero-is-under-one-tick-not-
unmeasured.md`, C3) -- every one of the 18 rows this plan's own C3 chunk
readjudicated carries a `c2_citation` dict, the sole traceable link between this
table's hand-curated `disposition` prose and a re-runnable measurement. It is
NOT a re-adjudication of the row -- 17 of the 18 rows are gravestones, and a
gravestone is never reinstated whatever a fresh figure says (module docstring
above). It exists so a reader doubting a `disposition`'s number can re-run
`coordinator_core.telemetry.op_adjudication.adjudicate` at the SAME `t_start`
bounds and get the same figure back, rather than trusting a hand-copied number
with no instrument behind it.

    {"route": <str|None>, "confidence": "EXACT"|"FLOOR"|"SPAWNS-UNKNOWN"|None,
     "n": <int>, "p95_ms": <float|None>, "window": "all_time",
     "t_start_min": <ISO-8601 str|None>, "t_start_max": <ISO-8601 str|None>,
     "verdict": "adjudicated"|"unadjudicated"|"no_rows_in_window",
     "outcome": "re-affirmed"|"reinstated"|"re-classified"}

Selection rule: per op, the single `(route, confidence)` bucket from
`op_adjudication.adjudicate`'s all-time arm carrying the largest `n` --
not necessarily the bucket `op_verdicts` would convict on, since several of
these rows have no `EXACT`/`FLOOR` bucket large enough to convict at all and
citing the largest-n bucket regardless of confidence is more informative than
citing nothing. `route`/`confidence`/`n`/`p95_ms`/`t_start_min`/`t_start_max`
are all `None` and `verdict` reads `"no_rows_in_window"` for an op with zero
matching rows in the query's population (`ceremony.post_commit_tail`,
`write_surface.emit_manifest`) -- per this plan's own instruction, absence of
rows is a statement about the window, never an inference that the op got
faster.

`outcome` is `"re-affirmed"` for all 18: every row's own `disposition` prose
predates this citation and is unchanged by it -- 17 gravestones stay dead
(kill means kill forever, module docstring above) and the one non-gravestone
(`fleet.archive_completed_plans`, whose own disposition already reads "not a
gravestone: the job is wanted, its host is open") is unchanged in kind. No row
in this batch met the bar for `"reinstated"` (permanently unavailable to a
gravestone) or `"re-classified"` (would require a row's own disposition prose
to have been wrong about what killed it, which C3's re-read did not find).

Spec backlink: docs/decisions/DR-349-one-budget-governs-every-constructed-op.md
               docs/problems/2026-08-21-the-over-budget-timeout-hitlist.md
               state/sizings/2026-08-21-lower-the-global-op-budget-to-2s-and-ado.yaml
               state/dispatch-briefs/2026-08-29-a-zero-is-under-one-tick-not-unmeasured/C3.md
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

__all__ = [
    "SUSPENSION_BAR_MS",
    "OCCUPANCY_BAR_SECS",
    "SUSPENDED_OPS",
    "OVER_BAR_OPS_PENDING_REMEDY",
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
        "c2_citation": {
            "route": "warm_server",
            "confidence": "EXACT",
            "n": 35,
            "p95_ms": 15.625,
            "window": "all_time",
            "t_start_min": "2026-08-25T21:34:55Z",
            "t_start_max": "2026-08-27T12:37:10Z",
            "verdict": "adjudicated",
            "outcome": "re-affirmed",
        },
        "measured": {"max_ms": 30016.6, "p50_ms": 30010.8, "n": 8},
        "note": "8/8 ended in caller_timeout at 30s.",
        "disposition": (
            "gravestone -- job was 'at session start, scan the whole "
            "work-state corpus and tidy it up'. It never once finished: 8 of "
            "8 calls hit the 30s ceiling and were cut off, so nothing has "
            "ever depended on an outcome it produced. Comes back only if a "
            "rebuild both completes inside budget AND something is shown to "
            "depend on its output -- neither condition has ever held."
        ),
        "spinoff": None,
    },
    # -----------------------------------------------------------------------
    # K-103 .. K-115 — the 200ms sweep, PM ruling 2026-08-27. (`review_trail.write`
    # is NOT part of this batch's numbering: it was already ruled a gravestone
    # today at K-060 under DR-372/DR-374, and this commit is the drain that entry
    # names as its follow-on. It rides along here for the registration work only.)
    #
    # The bar for these fourteen is 200ms of PROCESS time, not this table's
    # own SUSPENSION_BAR_MS (2000ms) and not DR-344's 500ms brightline. PM
    # ruling, verbatim across three turns: "Everything still over the bar gets
    # deleted", "any over 200ms get killed. deleted", "422ms for a commit is an
    # offender". This is the same threshold the `session.boot_sweep` gravestone
    # convicted on three commits earlier at 77341a0fa ("one process over 200ms
    # needs a fix, and this spends 6 processes on one archival batch"), so the
    # number is not new here — its application as a DELETE disposition is.
    #
    # Every row below is a gravestone, not a suspension: the code is drained
    # from the registration surfaces in the same commit. A name with no
    # implementation keeps the refusal loud instead of degrading to
    # METHOD_NOT_FOUND — the property the boot_sweep row exists to hold.
    #
    # UNIT IS STATED PER ROW, AND IT IS NOT UNIFORM. Eight rows carry process
    # time from the `process_ms` column of the op-latency sink. Six carry WALL
    # CLOCK only, because they have zero `process_ms` rows on this corpus —
    # they were never instrumented on the axis that governs. This module's own
    # header says a suspension justified by wall clock is not evidence, and
    # that judgement is not suspended for these six: they are convicted on the
    # PM's ruling with the evidence gap named, not concealed. The remedy if one
    # is ever disputed is to measure it, and the instrument exists
    # (`benchmarks/process_time.batched_process_time_ms`).
    #
    # EVERY process_ms FIGURE BELOW IS A FLOOR, NOT A CEILING. The axis is
    # `time.process_time()` (CPU time attributed to THIS interpreter), which
    # cannot see, and does not sum, a child process's own CPU time. An op that
    # spawns git or another subprocess pays a real cost this table's own
    # process_ms number does not carry -- the recorded figure floors the
    # op's true process cost rather than bounding it. This gap is silent on
    # nine rows (the eight `process_ms` rows below plus `review_trail.write`'s
    # `process_ms_cold`); the six WALL_CLOCK rows already name their own,
    # different gap in the comment above.
    #
    # WHICH OF THE NINE SPAWN AT ALL -- the cheap discriminator that decides
    # whether the floor gap above is live or moot for a given row (read
    # directly off each row's implementation this session, not inferred):
    #   spawns a subprocess:     ceremony.commit (commit_pipeline.py, git),
    #                            handoff.archive_transition (git_native._git),
    #                            review_trail.write (subprocess.run),
    #                            deliverable.cascade_terminal (2 git spawns
    #                            PER ADVANCED CANDIDATE -- see below)
    #   does NOT spawn:          write_surface.emit_manifest,
    #                            fleet.prune_closed_bugs, roadmap.serve,
    #                            handoff.reconcile_open
    #   unestablished:           ceremony.post_commit_tail -- it CALLS
    #                            deliverable.cascade_terminal's retained
    #                            compute in-process, so it inherits that
    #                            row's spawns by composition. Listed as
    #                            unestablished rather than moved: this
    #                            session measured the cascade, not the tail.
    # CORRECTED 2026-08-30 (state/audits/2026-08-30-the-cascade-16ms-figure-
    # measured-a-no-op.md): `deliverable.cascade_terminal` was recorded here
    # as non-spawning off a re-measurement that had advanced zero candidates.
    # It spawns `git log` + `git status` per advanced candidate, unbatched,
    # via archive_stamp.stamp_shipped_in's scope-derived leg. A row read off
    # an implementation at a shape where the work does not run reads as
    # spawn-free for the same reason it reads as fast.
    # The four that do not spawn are fine exactly as recorded -- their
    # process_ms figure already covers the whole of their cost, because there
    # is no child process for `time.process_time()` to miss. The floor
    # caveat has teeth on the four that spawn, and on the tail that composes
    # one of them.
    "ceremony.post_commit_tail": {
        "c2_citation": {
            "route": None,
            "confidence": None,
            "n": 0,
            "p95_ms": None,
            "window": "all_time",
            "t_start_min": None,
            "t_start_max": None,
            "verdict": "no_rows_in_window",
            "outcome": "re-affirmed",
        },
        "measured": {"max_ms": 1937.5, "p50_ms": 421.9, "n": 241, "unit": "process_ms"},
        "note": (
            "K-116, a consequence of the same sweep rather than its own measurement: "
            "this op resolved deliverable.cascade_terminal and "
            "session.sweep_consumed_handoffs, both killed alongside it, so every "
            "dispatch raised OpSuspendedError. The figures carried here are "
            "ceremony.commit's -- the only path that reached it -- and are labelled "
            "as inherited rather than passed off as this op's own. A registered op "
            "that cannot succeed is worse than a gravestone. `run()` is retained "
            "undecorated: wsc_tail.py calls it directly and never went through the "
            "handler, so the in-process tail is unchanged."
        ),
        "spinoff": None,
    },
    "write_surface.emit_manifest": {
        "c2_citation": {
            "route": None,
            "confidence": None,
            "n": 0,
            "p95_ms": None,
            "window": "all_time",
            "t_start_min": None,
            "t_start_max": None,
            "verdict": "no_rows_in_window",
            "outcome": "re-affirmed",
        },
        "measured": {"max_ms": 1562.5, "p50_ms": 1453.1, "n": 6, "unit": "process_ms"},
        "note": (
            "AST-parses every module under _SCAN_ROOTS and imports each candidate "
            "on every dispatch. This module's own docstring claimed DoE reads the "
            "manifest in a lockstep test; that was checked at DoE 042963e67 "
            "(work/machine-a/2026-08-22) and is STALE — every hit there is archive "
            "prose, a sent memo, or a sizing marked CLEARED 2026-08-06. No live "
            "consumer, in either repo."
        ),
        "disposition": (
            "gravestone -- job was 'publish a machine-readable list of the "
            "files this engine is allowed to write, so the sibling repo can "
            "check nobody wrote outside it'. Nothing reads it: checked in "
            "DoE at a named commit, every hit was archive prose or a "
            "cleared record, and the claimed consumption was stale "
            "docstring, not a live check. Comes back only if a real "
            "consumer of the manifest is found or built in a sibling repo -- "
            "not by the manifest existing again."
        ),
        "spinoff": None,
    },
    "deliverable.cascade_terminal": {
        "c2_citation": {
            "route": "warm_server",
            "confidence": "EXACT",
            "n": 5,
            "p95_ms": 1218.75,
            "window": "all_time",
            "t_start_min": "2026-08-26T22:19:04Z",
            "t_start_max": "2026-08-27T16:22:47Z",
            "verdict": "unadjudicated",
            "outcome": "re-affirmed",
        },
        "measured": {"max_ms": 1218.8, "p50_ms": 523.4, "n": 4, "unit": "process_ms"},
        "note": (
            "Compute retained as a library in ops/deliverable_cascade.py for the "
            "in-process callers (plan_status_transition._run_cascade); only the "
            "dispatchable op is dead."
        ),
        "disposition": (
            "JOB STILL DONE, RELOCATION OVER THE BAR. The compute runs in "
            "post_commit_tail._run_deliverable_cascade and plan_status_transition."
            "_run_cascade. DISCREPANCY RESOLVED 2026-08-30 AGAINST THE RELOCATION "
            "(state/audits/2026-08-30-the-cascade-16ms-figure-measured-a-no-op.md): "
            "the 523.4ms p50 above REPRODUCES. The prior '16.3ms/call process, 0 "
            "spawns' figure measured a zero-candidate no-op — 0 spawns is only "
            "reachable when nothing advances, because the spawns are emitted by "
            "the per-candidate advance itself. Measured at the same in-process "
            "call shape against a 275-file handoff corpus, 2 runs: ~210ms process "
            "and exactly 2 git spawns PER ADVANCED CANDIDATE on a ~47ms scan base "
            "— 203-219ms at 1 candidate, 453-484ms at 2, 719-750ms at 3, 23.4s / "
            "200 spawns at 100. Over the 200ms bar from one candidate, over the "
            "DR-344 500ms brightline from three. The two spawns are "
            "archive_stamp.stamp_shipped_in's scope-derived leg (git log + git "
            "status, each scoped to that candidate's own scope paths), unbatched: "
            "the amplification class, reached through a library call rather than "
            "a dispatch. Nominated for its own kill-bar item, not shaved here — "
            "the fix is a batched log/status over the union of candidate scope "
            "paths, a rebuild of that leg rather than an edit."
        ),
        "spinoff": None,
    },
    "fleet.prune_closed_bugs": {
        "c2_citation": {
            "route": "warm_server",
            "confidence": "EXACT",
            "n": 6,
            "p95_ms": 828.125,
            "window": "all_time",
            "t_start_min": "2026-08-27T09:54:29Z",
            "t_start_max": "2026-08-29T11:28:28Z",
            "verdict": "unadjudicated",
            "outcome": "re-affirmed",
        },
        "measured": {"max_ms": 828.1, "p50_ms": 468.8, "n": 2, "unit": "process_ms"},
        "note": "n=2 — thin, and recorded as thin rather than rounded up.",
        "disposition": (
            "gravestone -- job was 'delete closed bug entries from the "
            "backlog file'. Housekeeping on a file nothing blocks on, "
            "evidenced by only 2 calls total. Comes back only if the "
            "backlog grows to a size a person stops noticing and tidying "
            "by hand -- not observed."
        ),
        "spinoff": None,
    },
    "ceremony.commit": {
        "c2_citation": {
            "route": "warm_server",
            "confidence": "EXACT",
            "n": 255,
            "p95_ms": 1562.5,
            "window": "all_time",
            "t_start_min": "2026-08-26T19:48:39Z",
            "t_start_max": "2026-08-30T10:15:28Z",
            "verdict": "adjudicated",
            "outcome": "re-affirmed",
        },
        "measured": {"max_ms": 1937.5, "p50_ms": 421.9, "n": 241, "unit": "process_ms"},
        "note": (
            "The repo's own commit route. ~75ms of the 421.9 is interpreter start "
            "plus module import (measured k=20: bare 25.8ms, +commit_op 75.0ms), "
            "so ~347ms is its own work — NOT the boot_sweep shape where the "
            "mechanism was never the dominant term. Deleting it does recover real "
            "process time. What still needs doing: something must commit."
        ),
        "fallback": (
            "No sanctioned in-repo route remains. Use git directly with an "
            "explicit pathspec (never a bare `git commit` — it swallows peer "
            "staged files on this shared tree)."
        ),
        "disposition": (
            "REQUIREMENT DISCHARGED — the note's own 'what still needs doing: "
            "something must commit' is answered. ceremony.commit_v2 is live and "
            "dispatchable (767079e6e), and it calls git/commit.py::commit_paths at "
            "ops/ceremony/commit_v2.py:146 — so commit_paths, written and measured "
            "at 0.00 spawns / 3.984ms process on the common case against 5.08 / "
            "67.500ms for the `git add` + `git commit` architecture it replaces, is "
            "no longer the caller-less v2 it was at d20d56893. THE FALLBACK ABOVE IS "
            "THEREFORE STALE: dial ceremony.commit_v2 rather than reaching for bare "
            "git. NOT a drop-in for every shape the old route accepted — CR bytes "
            "under an eol=crlf pin and text/eol-attributed paths reach a batched "
            "fallback rather than the zero-spawn path, and the 2 procs on the "
            "new-file arm are explicit_stage's `git check-ignore -v`, deliberately "
            "retained because a wrong gitignore match fails SILENTLY and commits a "
            "file the operator deliberately ignored. NOR A SEMANTIC DROP-IN, and "
            "this row's readers are the exact affected audience: when a path named "
            "in the pathspec is ALSO staged, commit_v2 commits the WORKTREE bytes "
            "while the route it replaces committed the STAGED blob. Measured both "
            "arms, same pathspec, opposite content in HEAD. A caller preserving a "
            "deliberate partial hunk must name those paths in `prefer_staged` "
            "(threaded through commit_v2's params) — nothing infers it, and "
            "index-differs-from-worktree is explicitly NOT the discriminator "
            "(git/commit.py invariant 1: equally true of an ordinary unstaged edit). "
            "state/bug-backlog/2026-08-27-commit-v2-cutover-silently-flips-whose-c-"
            "09cf57f3b909.yaml."
        ),
        "spinoff": None,
    },
    # eol.census — K-062 gravestoned this op id on 2026-08-27 but left it with
    # no roster row, so dispatch answered METHOD_NOT_FOUND: true, and useless. A
    # caller learns nothing from it about why the id is gone.
    #
    # eol.audit_producers, cut in the same K-entry, deliberately gets NO row: it
    # was cut on redundancy, never on cost, and this roster is a record of cost
    # convictions. A row here with a 0ms "breach" is a dial in disguise — the
    # ratchet's own evidence guard says so, and it is right.
    "eol.census": {
        "c2_citation": {
            "route": "in_process",
            "confidence": "EXACT",
            "n": 1,
            "p95_ms": 203.125,
            "window": "all_time",
            "t_start_min": "2026-08-27T16:12:11Z",
            "t_start_max": "2026-08-27T16:12:11Z",
            "verdict": "unadjudicated",
            "outcome": "re-affirmed",
        },
        "measured": {"max_ms": 30007.0, "p50_ms": 30007.0, "n": 0, "unit": "WALL_CLOCK"},
        "note": (
            "p95 30,007ms -- CEILING-DOMINATED, i.e. hitting the invocation "
            "timeout rather than completing "
            "(docs/problems/2026-08-21-the-over-budget-timeout-hitlist.md)."
        ),
        "disposition": (
            "gravestone (K-062). Never a separate id again: `repair(mutate=false)` "
            "WAS this op, reached by a second registration over one mechanism. "
            "The surviving op was itself killed the same day (see eol.repair "
            "below), so the whole family is gone and the JOB is owed a v2 that "
            "works at the write, not over the corpus."
        ),
        "spinoff": None,
    },
    "eol.repair": {
        "c2_citation": {
            "route": "in_process",
            "confidence": "SPAWNS-UNKNOWN",
            "n": 9,
            "p95_ms": 328.125,
            "window": "all_time",
            "t_start_min": "2026-08-27T16:12:10Z",
            "t_start_max": "2026-08-27T17:02:09Z",
            "verdict": "unadjudicated",
            "outcome": "re-affirmed",
        },
        "measured": {"max_ms": 656.2, "p50_ms": 632.7, "n": 3, "unit": "process_ms"},
        "note": (
            "609-656ms cold end-to-end through the invoke entrypoint, 219-313ms "
            "of the calling process's own CPU warm, both on the normal-tier box "
            "with nothing contending -- over the 500ms brightline on the sunniest "
            "reading. Corroborated far above that in production: p95 30,007ms in "
            "docs/problems/2026-08-21-the-over-budget-timeout-hitlist.md, i.e. "
            "ceiling-dominated -- timing out rather than working."
        ),
        "disposition": (
            "gravestone -- the JOB survives, this shape does not. Job was 'a "
            "declared eol= must match the bytes on disk for anything "
            "executable', and it is real: the dry run found a .cmd launcher "
            "declared crlf sitting LF-only, a class git cannot show you because "
            "it normalizes the blob. But the op answered it by reading the whole "
            "corpus, and it is OpClass.MUTATING, so the warm engine held its "
            "single process-global write lock -- every commit and ceremony write "
            "fleet-wide -- for an UNCAPPED O(corpus) census. 42 violations found, "
            "41 of them scratch. The question is answerable at the write, on the "
            "handful of paths a commit touches. PM 2026-08-27: 'better to kill "
            "that, 580ms, and try to have a v2 that isn't a resource suck.' "
            "v2 is a fresh plan from first principles, never a refactor of this."
        ),
        "spinoff": None,
    },
    "roadmap.serve": {
        "c2_citation": {
            "route": "warm_server",
            "confidence": "SPAWNS-UNKNOWN",
            "n": 609,
            "p95_ms": 484.375,
            "window": "all_time",
            "t_start_min": "2026-08-21T22:05:13Z",
            "t_start_max": "2026-08-29T16:13:06Z",
            "verdict": "adjudicated",
            "outcome": "re-affirmed",
        },
        "measured": {"max_ms": 578.1, "p50_ms": 406.2, "n": 585, "unit": "process_ms"},
        "note": "n=585, the best-evidenced row in this batch.",
        "disposition": (
            "gravestone -- but the JOB survives; only claude-klabauter's ownership of "
            "it does not. Job was 'answer questions about the roadmap on "
            "demand'. PM: 'can be handled by example-retrieval-repo, they do lots of "
            "query stuff.' 585 calls made this the most-used row in the "
            "whole set, and it is still not claude-klabauter's job -- a query surface "
            "belongs to the repo that owns querying. This routes to "
            "example-retrieval-repo as a cross-repo note, not a v2 owed here."
        ),
        "spinoff": None,
    },
    "handoff.reconcile_open": {
        "c2_citation": {
            "route": "in_process",
            "confidence": "SPAWNS-UNKNOWN",
            "n": 21,
            "p95_ms": 5546.875,
            "window": "all_time",
            "t_start_min": "2026-08-25T21:31:31Z",
            "t_start_max": "2026-08-27T16:24:13Z",
            "verdict": "unadjudicated",
            "outcome": "re-affirmed",
        },
        "measured": {"max_ms": 7250.0, "p50_ms": 320.3, "n": 42, "unit": "process_ms"},
        "note": (
            "Wall clock read p50 16193.2ms against 320.3ms process — a 50x gap "
            "that is peer load, not this op. Convicted on the process figure."
        ),
        "spinoff": None,
    },
    "handoff.archive_transition": {
        "c2_citation": {
            "route": "warm_server",
            "confidence": "EXACT",
            "n": 19,
            "p95_ms": 265.625,
            "window": "all_time",
            "t_start_min": "2026-08-25T21:38:05Z",
            "t_start_max": "2026-08-27T09:49:14Z",
            "verdict": "unadjudicated",
            "outcome": "re-affirmed",
        },
        "measured": {"max_ms": 828.1, "p50_ms": 250.0, "n": 24, "unit": "process_ms"},
        "note": "Closest to the line of the process-measured rows; 250.0 > 200.",
        "spinoff": None,
    },
    "handoff.housekeeping": {
        "measured": {"max_ms": 2046.9, "p50_ms": 2046.9, "n": 1, "unit": "process_ms"},
        "note": (
            "The job that replaced the three keys above, killed on the same bar "
            "they were. 2046.9ms process at entry=op:handoff.housekeeping over a "
            "fixture carrying the real corpus's shape, against a 200ms criterion "
            "-- 10x, and the same order as the audit's independently measured "
            "~2300ms. Measured by the plan's own falsifier at baseline_ref "
            "bcfe23e13, which is the same instrument that reports 109.4ms for "
            "the successor. A GRAVESTONE, not a suspension: the module and all "
            "four registration entries were deleted in f4b9e53f5, so this row "
            "is a name with no code behind it, keeping the refusal loud rather "
            "than letting it degrade to METHOD_NOT_FOUND."
        ),
        "fallback": (
            "housekeeping.cycle does the same job -- it takes the same close/"
            "transition/cap parameters and returns the transition leg's result "
            "verbatim, so a caller's own predicates are unchanged."
        ),
        "successor_live": True,
        "spinoff": None,
    },
    "review_trail.write": {
        "c2_citation": {
            "route": "warm_server",
            "confidence": "SPAWNS-UNKNOWN",
            "n": 683,
            "p95_ms": 62.5,
            "window": "all_time",
            "t_start_min": "2026-08-21T21:09:09Z",
            "t_start_max": "2026-08-29T11:17:11Z",
            "verdict": "adjudicated",
            "outcome": "re-affirmed",
        },
        "measured": {"max_ms": 212.5, "p50_ms": 212.5, "n": 1, "unit": "process_ms_cold"},
        "note": (
            "Dead on DR-372/DR-374, NOT on the 200ms bar — kill-ledger K-060 "
            "already ruled this surface a gravestone on 2026-08-27 and named "
            "this drain as its follow-on chunk. Recorded because the process "
            "figures alone would not have carried it: 212.5ms cold / 46.9ms "
            "warm, and warm is how it runs. It was suspended once before on "
            "wall clock and the PM reinstated it 2026-08-23; that reversal is "
            "not being repeated here on the same evidence, it is superseded by "
            "a separate ruling."
        ),
        "fallback": (
            "A review IS recordable — dispatch a review agent and its filled "
            "sidecar is the receipt (DR-372). Blank sidecar means the review "
            "aborted; filled means it ran. Nothing is hand-recorded, and no "
            "replacement op is owed."
        ),
        "successor_live": True,
        "spinoff": None,
    },
    # --- convicted WITHOUT process-time evidence (wall clock only) ----------
    "session.sweep_consumed_handoffs": {
        "c2_citation": {
            "route": "warm_server",
            "confidence": "EXACT",
            "n": 1,
            "p95_ms": 0.0,
            "window": "all_time",
            "t_start_min": "2026-08-27T13:05:37Z",
            "t_start_max": "2026-08-27T13:05:37Z",
            "verdict": "unadjudicated",
            "outcome": "re-affirmed",
        },
        "measured": {"max_ms": 104963.8, "p50_ms": 17411.8, "n": 198, "unit": "WALL_CLOCK"},
        "note": "No process_ms rows exist for this op. Never instrumented.",
        "spinoff": None,
    },
    "cartography.churn": {
        "c2_citation": {
            "route": "in_process",
            "confidence": "EXACT",
            "n": 2,
            "p95_ms": 0.0,
            "window": "all_time",
            "t_start_min": "2026-08-27T16:10:01Z",
            "t_start_max": "2026-08-27T16:24:13Z",
            "verdict": "unadjudicated",
            "outcome": "re-affirmed",
        },
        "measured": {"max_ms": 2462.0, "p50_ms": 2462.0, "n": 1, "unit": "WALL_CLOCK"},
        "note": "n=1. One sample, wall clock, no process instrumentation.",
        "disposition": (
            "gravestone -- job was 'report which parts of the codebase are "
            "churning most'. One call, ever. No implementation and no "
            "caller survive, and nothing ever consumed the answer. Comes "
            "back only if a consumer of a churn report is named first -- "
            "not by rebuilding the report on spec."
        ),
        "spinoff": None,
    },
    "handoff.has_live_children": {
        "c2_citation": {
            "route": "warm_server",
            "confidence": "EXACT",
            "n": 54,
            "p95_ms": 359.375,
            "window": "all_time",
            "t_start_min": "2026-08-25T21:38:05Z",
            "t_start_max": "2026-08-29T16:12:20Z",
            "verdict": "adjudicated",
            "outcome": "re-affirmed",
        },
        "measured": {"max_ms": 7120.0, "p50_ms": 1666.6, "n": 233, "unit": "WALL_CLOCK"},
        "note": (
            "Compute retained UNDECORATED in ops/handoff_children.py — "
            "handoff_close_origin_stub._try_close needs the children payload that "
            "has_live_children_many does not return. Do not restore the decorator. "
            "CHILDREN-PAYLOAD QUESTION CLOSED 2026-08-30 (hnd-handoff-has-live-"
            "children-comp-b26358): no gap. _try_close imports the single-candidate "
            "compute directly (handoff_close_origin_stub.py:250), calls it at :987 "
            "and reads guard_res['children'] at :1018; the compute returns that key "
            "on every reply including the fail-closed one. No batch path reaches "
            "_try_close — has_live_children_many has exactly one caller "
            "(reap_in_flight_claims.py:361), which never enters this route, so the "
            "drift the spinoff was minted to test does not exist. The compute has "
            "FIVE in-process consumers, not one: _try_close, handoff_transition.py"
            ":888, deliverable_cascade.py:655, fleet/migrate_handoff_vocabulary.py"
            ":983, workstream_complete:2555 — the scope limit below is wider than "
            "it reads. The live cost is NOT this verdict but the corpus walk under "
            "it: _collect_handoff_paths measures 218.8ms process time over 1165 "
            "paths (275 live + 878 archived), and two sites re-walk it PER ITEM in "
            "a loop — post_commit_tail.py:834-839 per stamped baton (commit hot "
            "path) and migrate_handoff_vocabulary.py:981 per target. Three batons "
            "on one commit is 656ms process, over DR-344. Hoisting that walk is the "
            "open item; the verdict itself is not what costs."
        ),
        "disposition": (
            "GRAVESTONE, SCOPED TO THE ARCHIVE-TIME CHECK — not to the compute. PM "
            "ruling 2026-08-27, verbatim in-session: "
            "'either the handoff is done or it's not... why would we check if "
            "there are live children, ever?'. Two independent grounds, verified "
            "before the ruling was applied. (1) Archiving strands nothing: "
            "ceremony/resolver.py scans state/handoffs/ AND archive/handoffs/**, "
            "dag.py resolves targets ever tracked under archive/handoffs/YYYY-MM/, "
            "and baton_assemble resolves a moved predecessor silently — a child's "
            "pointer to an archived parent keeps resolving. (2) The guard INVERTS "
            "the contract it guards: handoff_archive_transition permits the git-mv "
            "only when deployment_state is already terminal, and 'continued' — the "
            "has-a-child state — is terminal, so the contract already says archive "
            "it while this guard refuses exactly that case. Measured 218.4ms/call "
            "process at its surviving call site (6 windows, 165-247ms, 0 spawns, "
            "state/audits/2026-08-27-what-the-laundered-libraries-cost-at-their-"
            "new-homes.md) — cost spent on a question with no consumer. Removal "
            "belongs to the housekeeping requirement carried by "
            "pln-one-corpus-read-or-the-houseke-18d29a; unwind baton_drift_sweep.py"
            ":271/:515 and consumed_handoff_stamp.py:505 there, not here. "
            "SCOPE LIMIT, and it is a real one: the ruling argues the ARCHIVE-TIME "
            "CHECK has no consumer. It does NOT reach the compute in "
            "ops/handoff_children.py, which has a separate consumer the argument "
            "never addressed — handoff_close_origin_stub imports "
            "_handoff_has_live_children (:250) and gates on its exit codes (:128-131), "
            "needing a children payload has_live_children_many does not return. "
            "K-113 says exactly that and is NOT superseded on the compute. Killing the "
            "guard and killing the compute are different deletions; only the first is "
            "argued for here."
        ),
        "spinoff": None,
    },
    "handoff.reconcile_close_terminal": {
        "c2_citation": {
            "route": "in_process",
            "confidence": "EXACT",
            "n": 1,
            "p95_ms": 0.0,
            "window": "all_time",
            "t_start_min": "2026-08-27T16:24:13Z",
            "t_start_max": "2026-08-27T16:24:13Z",
            "verdict": "unadjudicated",
            "outcome": "re-affirmed",
        },
        "measured": {"max_ms": 27947.1, "p50_ms": 3507.5, "n": 8, "unit": "WALL_CLOCK"},
        "note": "Module deleted outright; no non-test importers.",
        "disposition": (
            "gravestone -- job was 'close out a handoff whose work already "
            "landed, then file it'. Module deleted outright, no importers "
            "(re-verified at HEAD 2026-08-30). CORRECTED 2026-08-30: this "
            "row previously sent the requirement to the handoff-housekeeping "
            "family (handoff.reconcile_open et al.), which is itself CUT "
            "(K-057/K-108) with its carrier modules deleted -- a pointer to "
            "nothing. The job's two legs land on live surfaces instead: the "
            "close leg on handoff.transition action 'close' (archive-stamp-cli "
            "close-handoff --reason displaced, the same _close internal the "
            "deleted op composed), the filing leg on "
            "coordinator_core/housekeeping/cycle.py steps D+E via "
            "ops/fleet/_common.py::archive_and_commit. What died is the "
            "composite, not the job; the absence costs one extra call and "
            "makes no answer wrong. Full requirement test: kill-ledger K-025."
        ),
        "spinoff": None,
    },
    "merge_assemble.brief": {
        "c2_citation": {
            "route": "warm_server",
            "confidence": "EXACT",
            "n": 34,
            "p95_ms": 31.25,
            "window": "all_time",
            "t_start_min": "2026-08-26T21:33:35Z",
            "t_start_max": "2026-08-27T10:47:10Z",
            "verdict": "adjudicated",
            "outcome": "re-affirmed",
        },
        "measured": {"max_ms": 1357.2, "p50_ms": 1087.3, "n": 52, "unit": "WALL_CLOCK"},
        "note": "merge_assemble.apply survives in the same module and is untouched.",
        "disposition": (
            "gravestone -- job was 'summarise what is in a branch before "
            "merging it'. Not convicted on performance: PM, verbatim, "
            "'generated summary is weird'. The job does not need doing at "
            "all -- a person reads the diff. merge_assemble.apply (the "
            "other half of the same module) is unaffected and stays. Comes "
            "back only if the PM decides the summary itself is wanted "
            "again, never by making generation faster."
        ),
        "spinoff": None,
    },
    "fleet.archive_completed_plans": {
        "c2_citation": {
            "route": "warm_server",
            "confidence": "EXACT",
            "n": 2,
            "p95_ms": 0.0,
            "window": "all_time",
            "t_start_min": "2026-08-27T13:05:37Z",
            "t_start_max": "2026-08-27T13:05:37Z",
            "verdict": "unadjudicated",
            "outcome": "re-affirmed",
        },
        "measured": {"max_ms": 27940.0, "p50_ms": 996.1, "n": 246, "unit": "WALL_CLOCK"},
        "note": (
            "Resolved in-process by ceremony/commit_pipeline.py and tail_ops.py; "
            "compute retained as a library, dispatchable op dead."
        ),
        "disposition": (
            "NOTE ABOVE IS STALE — the cited call site no longer exists. "
            "commit_pipeline._run_in_plane_archive_sweep, its cadence gate, its "
            "cap accessor, _resync_shared_index_for_swept_paths and "
            "consumed_handoff_stamp._fire_consumed_handoff_sweep were all deleted "
            "at abd587695 (2026-08-27) on the PM ruling 'I do not want and will "
            "not accept commits being saddled with something like this... take "
            "them off the commit path. I do want automated housekeeping of this "
            "stuff, that's very important to me.' So this compute runs NOWHERE on "
            "the commit path now and could not be measured there. The housekeeping "
            "REQUIREMENT survives that removal by the ruling's own words and is "
            "carried by pln-one-corpus-read-or-the-houseke-18d29a, which owns "
            "finding it a non-commit-path host. Not a gravestone: the job is "
            "wanted, its host is open."
        ),
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


# --- OTHER OVER-BAR OPS THE C2 QUERY FOUND (2026-08-30, plan `2026-08-29-a-
# zero-is-under-one-tick-not-unmeasured.md`, C4) ---------------------------
#
# NOT a second SUSPENDED_OPS, and membership here does nothing operational.
# Membership in SUSPENDED_OPS refuses dispatch (module docstring above); the
# plan that produced this table draws a hard line between CONVICTING an op --
# writing down that its own evidence puts it over a brightline bar -- and
# REMEDYING it -- suspending, gravestoning, or fixing it, which that plan
# names explicitly out of scope: "Fixing any op this plan convicts.
# Convicting is the deliverable; each conviction's remedy is its own plan
# under the kill-bar rule." An entry below is evidence for a reader, exactly
# like this module's own `measured` fields, never a refusal.
#
# `records.query` is the one entry here entitled to `convicted: True`: EXACT
# confidence, n=328 >= 30, and it breaches the 500ms kill bar on its own
# current-traffic figure. Every other op below carries `convicted: False` --
# the chunk that wrote this table is explicit that recording a thin (n < 30)
# or floored (spawns > 0) figure AS a conviction "is the failure this chunk
# exists to avoid".
OVER_BAR_OPS_PENDING_REMEDY: Dict[str, Dict[str, object]] = {
    "records.query": {
        "convicted": True,
        "bar": "kill",
        "measured": {
            "p95_ms": 859.4,
            "n": 328,
            "window": "24h_production",
            "unit": "process_ms",
        },
        "all_time": {"p95_ms": 312.5, "n": 831},
        "confidence": "EXACT",
        "route": None,
        "note": (
            "Absent from SUSPENDED_OPS entirely. p95 859.4ms over the 500ms "
            "kill bar on current 24h production traffic (n=328), trending "
            "worse than its 312.5ms all-time figure (n=831). EXACT: none of "
            "the 831 all-time rows carries a spawn, so the process-time "
            "figure is the op's real cost, not a floor. Convicted here; the "
            "remedy (suspend, gravestone, or fix) is a separate plan's job -- "
            "this chunk's own scope excludes fixing what it convicts."
        ),
    },
    "session.reap_claims_for_repos": {
        "convicted": False,
        "bar": "kill",
        "measured": {
            "p95_ms": 34265.6,
            "n": 4,
            "window": "24h_production",
            "unit": "process_ms",
        },
        "confidence": "UNADJUDICATED",
        "route": None,
        "note": (
            "n=4 (n=6 all-time). The most suspicious figure in the sweep -- "
            "either the worst op on the box or an artifact, and n=4 cannot "
            "tell you which. Recorded as unadjudicated, not convicted."
        ),
    },
    "handoff.housekeeping": {
        "convicted": False,
        "bar": "kill",
        "measured": {
            "p50_ms": 3000.0,
            "n": 4,
            "window": "24h_production",
            "unit": "process_ms",
        },
        "confidence": "UNADJUDICATED",
        "route": None,
        "note": (
            "This op NAME already carries its own SUSPENDED_OPS row above -- "
            "a gravestone, module and registrations deleted, successor "
            "housekeeping.cycle live. This is a SEPARATE figure: 24h "
            "production traffic still recorded against the dead name, n=4. "
            "Not reconciled here (out of scope) and not read as evidence the "
            "gravestone needs revisiting -- a gravestone is never reinstated "
            "regardless of what a fresh figure says (module docstring above)."
        ),
    },
    "session.safe_commit_offer": {
        "convicted": False,
        "bar": "kill",
        "measured": {
            "p95_ms": 984.4,
            "n": 56,
            "window": "24h_production",
            "unit": "process_ms",
        },
        "confidence": "FLOOR",
        "route": None,
        "note": (
            "n=56 is adjudicable, but the op SPAWNS (14 of 88 all-time rows "
            "carry a spawn, max 6), so the figure is a FLOOR -- child CPU is "
            "excluded, per the two universal spawn-floor lessons this plan's "
            "Problem section cites. Recorded as over the 500ms kill bar with "
            "the floor stated, never as a measured cost."
        ),
    },
    "session.reap": {
        "convicted": False,
        "bar": "kill",
        "measured": {
            "p95_ms": 625.0,
            "n": 6,
            "window": "24h_production",
            "unit": "process_ms",
        },
        "confidence": "UNADJUDICATED",
        "route": None,
        "note": "n=6. Recorded as unadjudicated, not convicted.",
    },
    "memo.send": {
        "convicted": False,
        "bar": "fix",
        "measured": {
            "p95_ms": 468.8,
            "n": 48,
            "window": "24h_production",
            "unit": "process_ms",
        },
        "confidence": "FLOOR",
        "route": None,
        "note": (
            "SPAWNS (54 of 136 all-time rows, max 2): a FLOOR over the 200ms "
            "fix bar. The floor alone does not establish where it sits "
            "against the 500ms kill bar -- child CPU is excluded and could "
            "carry it on either side of that line."
        ),
    },
    "session.warm_start": {
        "convicted": False,
        "bar": "fix",
        "measured": {
            "p50_ms": 406.2,
            "n": 140,
            "window": "24h_production",
            "unit": "process_ms",
        },
        "confidence": "EXACT",
        "route": "in_process",
        "note": (
            "Over the 200ms fix bar on its face, but this op is ALREADY "
            "doubly gravestoned (K-032, K-061) with its module deleted -- "
            "state/audits/2026-08-30-session-warm-start-closure-verification"
            ".md found an overlapping figure (p50 391.8ms, n=23, spawns: 0) "
            "to be telemetry residue of a caller still dispatching a "
            "METHOD_NOT_FOUND name, not a live op's cost, and records that "
            "leak as closed 2026-08-30T10:11:40Z. This chunk's larger n=140 "
            "reading was not individually re-verified against that closure "
            "timestamp -- recorded with that context stated rather than "
            "convicted as a fresh finding, and NOT read as a reason to "
            "revisit either gravestone."
        ),
    },
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
    fallback = ""
    note = ""
    if isinstance(record, dict):
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
        # THE BAR NAMED MUST BE THE BAR IT DIED ON. The 200ms sweep
        # (K-060..K-073) convicts on process time against a 200ms line;
        # rendering those rows against SUSPENDED_BAR_MS told the caller the op
        # was 1938ms into a 2000ms budget -- under it -- when the actual finding
        # was 421.9ms of process time against 200ms. Same defect class as the
        # instrument note above: the record was honest, the message was not.
        f"{method} is off: {_bar_clause(record)}. "
        + (f"How that number arose: {note} " if note else "")
        + (f"{fallback} " if fallback else "")
        + "Killed, not suspended -- the old implementation does not come back."
        + (
            ""
            if _successor_is_live(record)
            else " If the job is still needed, plan a new one under 200ms."
        )
    )


def _successor_is_live(record: object) -> bool:
    """True when a row's `fallback` names a ratified successor that already
    does the job, so the caller has somewhere to go right now.

    The closing `plan a new one under 200ms` is the correct disposition for a
    row whose job is genuinely unhomed, and the wrong one for a row whose job
    was rehomed by ruling — it sends a reader off to build what already
    exists. `review_trail.write` is the measured case: DR-372 rehomed the job
    to the dispatched-agent sidecar receipt, and two DoE sessions
    (doe-claude-1c and doe-claude-2e, 2026-08-27) independently read the
    refusal as a fleet-wide capability gap rather than as a pointer to the
    live mechanism. Same defect class as the two instrument notes above: the
    record was honest, the message was not.
    """
    return bool(isinstance(record, dict) and record.get("successor_live"))


#: The process-time line the 2026-08-27 sweep convicts on (PM ruling). Distinct
#: from `SUSPENSION_BAR_MS`, which is a wall-clock box-occupancy bar, and from
#: DR-344's 500ms brightline. Like both, it may be LOWERED, never raised.
PROCESS_BAR_MS: float = 200.0


def _bar_clause(record: object) -> str:
    """The `<figure> against a <bar>` clause, in the unit the row was judged in.

    A row carrying `measured.unit` starting with `process_ms` is a 200ms
    process-time conviction; `WALL_CLOCK` names itself as such so a reader can
    see the evidence gap rather than infer a process figure that was never
    taken; anything else falls back to the legacy `SUSPENSION_BAR_MS` framing
    the pre-2026-08-27 rows were written against.
    """
    measured = record.get("measured") if isinstance(record, dict) else None
    if not isinstance(measured, dict):
        return f"measured over a {SUSPENSION_BAR_MS:.0f}ms bar"
    unit = str(measured.get("unit") or "")
    try:
        p50 = float(measured.get("p50_ms") or 0.0)
        mx = float(measured.get("max_ms") or 0.0)
    except (TypeError, ValueError):
        p50 = mx = 0.0
    if unit.startswith("process_ms"):
        return (
            f"p50 {p50:.0f}ms process time against a {PROCESS_BAR_MS:.0f}ms bar"
        )
    if unit == "WALL_CLOCK":
        return (
            f"p50 {p50:.0f}ms WALL CLOCK against a {PROCESS_BAR_MS:.0f}ms process "
            "bar -- this op has no process-time measurement, and the gap is "
            "named rather than filled"
        )
    return f"max {mx:.0f}ms against a {SUSPENSION_BAR_MS:.0f}ms bar"
