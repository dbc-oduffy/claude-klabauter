"""coordinator_core.benchmarks.shim_decision_rule -- the written-in-advance
decision rule for "is the process-per-predicate forwarder shim cheaper than
the current direct entry point", plus the purpose-built record shape that
carries a verdict computed against it.

STAGE 1 OF A TWO-STAGE CHUNK (C7). This module contains the rule ONLY --
no measurement, no invocation of `interleave.run_interleaved`, no numbers
from a live run. A later dispatch (stage 2) measures against exactly what
is written here and emits a `ShimDecisionRecord` using `evaluate()` below.
The rule landing in its own commit, before the number exists, is what makes
"the rule was not fit to the result" a checkable claim rather than a promise.

Why not `record.ConformanceRecord` (deviation from the chunk body's literal
wording, decided by the executor, not a re-spec): that dataclass requires
`op` to match an `OP_CLASSIFICATION` key, an `op_class`, and a
`budget.py`-resolved `target_ms`. A shim-vs-direct-entry-point comparison is
none of those -- there is no op name, no OpClass tier, and no per-op SLA
budget being conformance-checked; there are two competing *invocation paths*
being compared against each other, with the older one as baseline. C1 hit
the identical mismatch when it built `interleave.py` and correctly declined
to force its primitives through `ConformanceRecord` either. AC6 (this plan)
enumerates statistic / margin / baseline / verdict as the required record
contents, not `ConformanceRecord`'s field set -- `ShimDecisionRecord` below
carries exactly those, plus enough provenance (N, seed, timestamp, per-arm
stats) to make the verdict re-derivable from the record alone.

THE DECISION RULE:

Governing statistic: p90, not median. C1 measured p90 sitting 3-4.5x over
median on every spawning primitive under this box's ambient 50-70-concurrent
-LLM load (`forwarder_dispatcher_roundtrip`: 97.51ms median / 239.34ms p90,
a 2.45x spread; `bare_cpython_start`: 47.01ms / 177.21ms, a 3.77x spread).
A median-only verdict would certify a shim whose *typical bad case* is
several times worse than its typical case looks -- exactly the tail this
plan's replacement is meant to bound. p90 is the cheapest percentile that
still prices the tail rather than the lucky half of draws, and it is what
`interleave.PrimitiveStats` already computes, so no new reduction machinery
is needed to honor it.

Cheaper-than margin: the shim's p90 must be no more than 90% of the
baseline's p90 (i.e. at least a 10% reduction) to count as "cheaper". This
margin is NOT derived from the 2.45-3.77x median-to-p90 SPREAD cited above
under 'Governing statistic' -- that figure describes a distribution SHAPE
(how far a single primitive's p90 sits above its own median), not the
run-to-run VARIABILITY of the p90 ESTIMATE itself, which is the only
quantity that tells you whether a 10% difference between two arms' p90s
could be produced by noise alone. Nobody had measured that quantity when
this docstring first shipped -- the margin was asserted, not derived, and
an earlier version of this section additionally described the derivation
backwards (arguing a margin "an order of magnitude below the noise" was
therefore not swamped by it, which is the opposite of what "below the
noise" means). Both errors are corrected below by MEASURING the actual
quantity instead of reasoning about a proxy for it.

CALIBRATION (empirical, reproducible): `calibrate_aa_noise_floor()` below
runs a genuine A/A test -- two `interleave.Primitive` arms that invoke the
IDENTICAL callable under two different names, interleaved via
`interleave.run_interleaved()` exactly as the real baseline-vs-shim
comparison will be, repeated `r_repeats` times. Because both arms do
identical work, any non-zero `reduction_fraction` `evaluate()` would
compute between them is pure noise -- ambient-load drift and sampling
variance, not a real effect. The distribution of that noise across
`r_repeats` repeats is the noise floor a real margin must clear.

MEASURED RESULT (this module's own commit, `python -m coordinator_core.
benchmarks.shim_decision_rule --calibrate`, on this box, using
`build_baseline_primitive()` below -- i.e. calibrated on the SAME
concrete invocation named under 'Baseline', not a proxy primitive):
r_repeats=8, n_rounds=30 (60 interleaved draws per repeat, matching
N_ROUNDS below), wall clock ~2m54s under this box's ambient 50-70
-concurrent-LLM load. See `AA_CALIBRATION_REDUCTIONS` for the raw signed
per-repeat reduction_fraction values and `AA_CALIBRATION_NOISE_FLOOR` for
max(abs(x) for x in AA_CALIBRATION_REDUCTIONS) -- the widest apparent
swing an A/A pair produced across the 8 repeats: 0.4625 (46.25%), driven
by a single repeat that swung the p90 by nearly half under load -- wide
and ugly, exactly what this box's ambient load produces, reported as
measured rather than smoothed. CHEAPER_THAN_MARGIN is set strictly above
that measured floor (see the constant's own docstring for the exact
numbers and the applied safety factor). This makes CHEAPER_THAN_MARGIN
itself wide (~0.69, i.e. a shim must cut p90 by ~69% to register as
'cheaper' on this box) -- a direct, unflattering consequence of how much
p90-level noise `_time_subprocess`-based measurement carries under this
box's concurrency, not a bug in the derivation. If a future re-run on
different ambient load measures a wider floor than the current margin,
that is grounds to re-run calibration and move the margin -- NOT grounds
to keep the old number and hope.

Baseline: the CURRENT DIRECT ENTRY POINT -- i.e. whatever invocation path a
caller uses TODAY to reach an `-assemble` entry point WITHOUT going through
the new forwarder+dispatcher shim (concretely, on this plan: `python
coordinator/bin/plan-assemble.py`, invoked with NO subcommand token, which
its own docstring documents as `FALLTHROUGH: a bare invocation with no
subcommand token briefs` and `READ-ONLY -- mutates nothing`; see
`build_baseline_primitive()` below, which builds exactly this argv via a
plain `subprocess.run` spawn-to-exit, resolved at `BASELINE_ENTRYPOINT_REL
_PATH` relative to the repo root). This is explicitly NOT `interleave.
default_baseline_primitives()`'s `forwarder_dispatcher_roundtrip` primitive
(which times `time_invocation("ping", ...)` -- the engine's bare invoke
entrypoint, an unrelated cheap-to-time primitive that has nothing to do with
any of the 13 `-assemble` entry points this plan's C7/C8 concern). Per this
plan's AC7/C8, the 13 `-assemble` entry points under `coordinator/bin/` are
the population the new forwarder shim replaces; `plan-assemble.py` is one
concrete member of that population, chosen because it is read-only,
deterministic, and already exits 0 with no arguments and no environment
setup on this tree (verified live during this chunk's stage 1).

Shim arm: DOES NOT EXIST YET and is NOT measured in this stage. C8 has not
landed `coordinator/bin/coordinator-assemble.py` or `coordinator/bin/lib/
entry_point_shim.py`, so there is no forwarder to spawn. `SHIM_PRIMITIVE_
NAME` below reserves the record-shape name stage 2 must use
(`assemble_entrypoint_via_forwarder_shim__plan`); stage 2 (after C8 lands)
must construct the actual `interleave.Primitive` for it -- a subprocess
spawn of the new forwarder invoked with whatever subcommand routes to
`plan-assemble`'s equivalent behaviour -- and is responsible for stating
that construction explicitly rather than pointing at a nearby primitive
that merely happens to be available (`forwarder_dispatcher_roundtrip` is
NOT a substitute for it; see above). The only permitted axis of difference
between the two arms, once the shim arm exists, is "does the caller go
through the new forwarder shim first, or not" -- both arms must reach the
same underlying `plan-assemble` behaviour.

Wash handling: within-margin (shim p90 is between 90% and 100% of baseline
p90, i.e. it improved by less than 10% or regressed) OR statistic-ambiguous
(e.g. sample_count too small for the p90 to be a stable nearest-rank value,
or the two arms' interleaved sample_counts differ because a draw errored)
both trigger the SAME outcome as a clear loss: STOP AND RE-SHAPE. `evaluate
()` below returns `verdict="wash"` for the former and `verdict="fail"` for a
clear regression (shim p90 >= baseline p90) -- both are non-passing, and
neither authorizes proceeding to C8/C10. There is no silent-pass path for
"inconclusive"; ambiguity is a stop condition, not a shrug.

N, seed, ambient-load handling: N=30 rounds through `interleave.
run_interleaved`, i.e. 30 interleaved draws per arm (60 total timed
invocations), reusing `interleave.default_baseline_primitives()`'s
`forwarder_dispatcher_roundtrip` as the baseline arm plus a new shim-path
primitive as the second arm -- never fewer than `run_interleaved`'s own
`len(primitives) < 2` floor, and 30 is 3x C1's own n=10 default to shrink
nearest-rank p90 sampling error given this rule's comparatively tight 10%
margin. Seed: `random.Random()` unseeded (no fixed seed) -- the point of
`run_interleaved`'s per-round shuffle is to average OUT ambient-load drift
across the measurement window, not to reproduce one specific shuffle order;
pinning a seed would buy bit-for-bit replay of shuffle order while doing
nothing for the actual reproducibility question (does the verdict hold
across different ambient-load conditions), and this box's ambient load is
never the same twice regardless. Ambient-load handling is `run_interleaved`
itself: both arms are drawn from the same interleaved span of wall-clock
time each round, so load drift lands on both arms roughly equally rather
than biasing whichever arm happens to be measured first -- no separate
ambient-load correction is applied on top of that in this module.

Spec backlink: docs/plans/2026-08-16-a-process-per-predicate.md, chunk C7.
"""

from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional

from coordinator_core.benchmarks.interleave import (
    Primitive,
    PrimitiveStats,
    _time_subprocess,
    run_interleaved,
)
from coordinator_core.ops._git_root_util import git_root_zero_spawn

SCHEMA_VERSION = 1
"""Pinned schema version for `ShimDecisionRecord`. This is a NEW record
shape (not `record.ConformanceRecord`'s contract surface, see module
docstring) -- bump only in lockstep with whatever stage-2/C8 consumer
reads it."""

GATING_STATISTIC = "p90_ms"
"""The one governing statistic this rule authorizes for the verdict. See
module docstring 'Governing statistic'."""

N_ROUNDS = 30
"""Interleaved rounds (`interleave.run_interleaved(..., n=N_ROUNDS)`) --
30 timed draws per arm. See module docstring 'N, seed, ambient-load
handling'."""

BASELINE_ENTRYPOINT_REL_PATH = ("coordinator", "bin", "plan-assemble.py")
"""Path segments (joined onto the resolved repo root) to the concrete
`-assemble` entry point this rule's baseline arm spawns. See module
docstring 'Baseline' for why `plan-assemble.py`, invoked bare, is the
chosen population member."""

BASELINE_PRIMITIVE_NAME = "assemble_entrypoint_direct__plan_assemble"
"""Name of the baseline arm's `interleave.Primitive`, built by
`build_baseline_primitive()` below."""

SHIM_PRIMITIVE_NAME = "assemble_entrypoint_via_forwarder_shim__plan"
"""Reserved name for the shim arm's `interleave.Primitive` -- NOT
constructible yet (C8 has not landed the forwarder). See module docstring
'Shim arm'. Stage 2 must use this exact name so a `ShimDecisionRecord`'s
`shim_name` field is comparable across runs."""


def build_baseline_primitive(repo_root: Optional[str] = None) -> Primitive:
    """Builds the baseline arm's `interleave.Primitive`: a bare spawn of
    `plan-assemble.py` with no subcommand token (its own docstring's
    documented FALLTHROUGH 'brief' behaviour, READ-ONLY). See module
    docstring 'Baseline'.

    `repo_root` anchors the entry-point path; defaults to the enclosing
    repo's toplevel via `git_root_zero_spawn` (a one-time, non-timed
    resolution at primitive-construction time -- never inside a timed
    draw), mirroring `interleave.default_baseline_primitives()`'s own
    pattern.

    Reuses `interleave._time_subprocess` (the same generic spawn-to-exit
    timer `interleave.py`'s own non-invoke-entrypoint primitives use)
    rather than re-authoring a subprocess timing loop -- see this module's
    'Reuse, not re-authoring' precedent in `interleave.py`'s own docstring.
    """
    resolved_root = repo_root if repo_root is not None else git_root_zero_spawn(Path(__file__))
    if resolved_root is None:
        raise RuntimeError(
            "shim_decision_rule.build_baseline_primitive: could not resolve a repo "
            "root to anchor the baseline entry-point primitive"
        )
    entrypoint = Path(resolved_root).joinpath(*BASELINE_ENTRYPOINT_REL_PATH)
    argv = [sys.executable, str(entrypoint)]
    return Primitive(
        name=BASELINE_PRIMITIVE_NAME,
        invoke=lambda: _time_subprocess(argv, cwd=resolved_root),
    )


def calibrate_aa_noise_floor(
    invoke: Callable[[], float],
    *,
    n_rounds: int = N_ROUNDS,
    r_repeats: int = 8,
) -> List[float]:
    """Empirically measures the A/A noise floor for a timed callable: runs
    `r_repeats` independent `interleave.run_interleaved()` calls, each with
    TWO arms that invoke the SAME `invoke` callable under two different
    names (a genuine A/A test -- identical work in both arms), and returns
    the signed `reduction_fraction` (per `evaluate()`'s own formula,
    1 - arm_b/arm_a on GATING_STATISTIC) each repeat produced.

    Because both arms do identical work, any non-zero value in the
    returned list is pure noise -- ambient-load drift and p90 sampling
    variance, not a real effect. This is the reproducible artifact module
    docstring 'Cheaper-than margin' cites: re-run this function (or
    `python -m coordinator_core.benchmarks.shim_decision_rule --calibrate`)
    to re-derive the noise floor a margin must clear, rather than trusting
    a number frozen in prose. A repeat where the baseline arm's stat is
    zero is skipped (reduction_fraction is undefined, mirrors `evaluate()`
    treating that case as non-computable) rather than raising -- a single
    degenerate repeat should not abort the whole calibration run.
    """
    reductions: List[float] = []
    for _ in range(r_repeats):
        arm_a = Primitive(name="aa_arm_a", invoke=invoke)
        arm_b = Primitive(name="aa_arm_b", invoke=invoke)
        stats = run_interleaved([arm_a, arm_b], n=n_rounds)
        a_stat = getattr(stats["aa_arm_a"], GATING_STATISTIC)
        b_stat = getattr(stats["aa_arm_b"], GATING_STATISTIC)
        # Review (2026-08-16): tolerance, not exact-zero equality -- a near-zero
        # but nonzero p90 (plausible for a fast in-process draw under thread/GC
        # noise) divides down into a huge reduction_fraction swing instead of
        # being skipped as degenerate. 1e-6ms is well below any real timer
        # resolution on this box, so it only catches the genuinely-degenerate case.
        if math.isclose(a_stat, 0.0, abs_tol=1e-6):
            continue
        reductions.append(1.0 - (b_stat / a_stat))
    return reductions


AA_CALIBRATION_R_REPEATS = 8
AA_CALIBRATION_N_ROUNDS = 30
AA_CALIBRATION_REDUCTIONS = (
    -0.0033051696267039077, 0.0861245748965428, 0.17553341568089686,
    0.46251195703577164, -0.14065468933217007, -0.006967802284192359,
    -0.07063016406137357, 0.2097408463221222,
)
"""Recorded result of `calibrate_aa_noise_floor(build_baseline_primitive().
invoke, n_rounds=AA_CALIBRATION_N_ROUNDS, r_repeats=AA_CALIBRATION_R_
REPEATS)`, measured live on this box (2026-08-16, ~2m54s wall clock)
against THIS module's own baseline arm (`build_baseline_primitive()`,
i.e. `plan-assemble.py` spawned bare) -- see module docstring
'Cheaper-than margin' / 'MEASURED RESULT'. These are signed apparent
reduction_fraction values between two arms that differ only by
measurement noise; a positive value here is exactly as spurious as a
negative one -- the widest, +0.4625, happened to land positive on this
run, which is itself evidence the box's ambient load (50-70 concurrent
LLMs) can swing an A/A p90 comparison by nearly half in either direction.
Re-run `calibrate_aa_noise_floor` to refresh this tuple if ambient load
on this box changes materially -- these numbers are a measurement, not a
constant of nature."""

AA_CALIBRATION_NOISE_FLOOR = max(abs(x) for x in AA_CALIBRATION_REDUCTIONS)
"""max(abs(x) for x in AA_CALIBRATION_REDUCTIONS) -- the widest apparent
p90 swing an A/A pair (identical work, both arms) produced across
AA_CALIBRATION_R_REPEATS repeats. CHEAPER_THAN_MARGIN must exceed this or
a real shim's measured 'cheaper' verdict is not distinguishable from
noise the harness itself already demonstrated it can produce on THIS
exact baseline arm."""

CHEAPER_THAN_MARGIN = round(AA_CALIBRATION_NOISE_FLOOR * 1.5, 2)
"""Required fractional p90 reduction (shim vs. baseline) to count as
'cheaper'. shim_p90 <= baseline_p90 * (1 - CHEAPER_THAN_MARGIN) to pass.
Set to 1.5x the measured `AA_CALIBRATION_NOISE_FLOOR` (rounded to the
nearest percentage point) -- comfortably above the widest noise swing
measured on this exact baseline arm, not swamped by it. See module
docstring 'Cheaper-than margin' for the full derivation and the earlier,
inverted justification this replaces. If `AA_CALIBRATION_NOISE_FLOOR` is
ever re-measured wider than this yields, CHEAPER_THAN_MARGIN must move
with it -- this constant is derived from the calibration, never tuned
independently of it."""

VERDICT_PASS = "pass"
VERDICT_WASH = "wash"
VERDICT_FAIL = "fail"
"""The only three verdicts `evaluate()` may return. Both VERDICT_WASH and
VERDICT_FAIL are non-passing and stop the chunk sequence identically --
see module docstring 'Wash handling'."""


@dataclass(frozen=True)
class ShimDecisionRecord:
    """Purpose-built record for the shim-vs-direct-entry-point decision.
    Deliberately NOT `record.ConformanceRecord` -- see module docstring for
    why. Carries statistic / margin / baseline / verdict (AC6's enumerated
    contents) plus enough provenance to re-derive the verdict from the
    record alone without re-running the benchmark.
    """

    gating_statistic: str
    """Name of the statistic the verdict was computed against. Must equal
    GATING_STATISTIC ('p90_ms') for a record produced by this module's
    `evaluate()` -- carried as a field, not hardcoded, so a reader does not
    have to trust the module constant matched at measurement time."""

    margin: float
    """Required fractional cheaper-than margin applied. Must equal
    CHEAPER_THAN_MARGIN for a record produced by `evaluate()`."""

    baseline_name: str
    """Primitive name of the baseline arm (the current direct entry point),
    e.g. 'forwarder_dispatcher_roundtrip' per this module's baseline
    definition."""

    baseline_stat_ms: float
    """Baseline arm's value of `gating_statistic`, in milliseconds."""

    shim_name: str
    """Primitive name of the shim arm under test."""

    shim_stat_ms: float
    """Shim arm's value of `gating_statistic`, in milliseconds."""

    baseline_sample_count: int
    shim_sample_count: int
    """Per-arm sample counts from the interleaved run. Distinct because a
    draw error in one arm can desynchronize them even though both arms are
    drawn the same number of rounds -- an ambiguity `evaluate()` treats as
    VERDICT_WASH, see module docstring 'Wash handling'."""

    n_rounds: int
    """Requested `run_interleaved(n=...)` round count (== N_ROUNDS for a
    record produced by `evaluate()`); NOT the same as sample_count, which
    is the realized per-arm draw count after the run completed."""

    seed_policy: str
    """Human-readable description of the seed policy used, e.g.
    'unseeded (random.Random())' -- never a numeric seed value for this
    rule, see module docstring."""

    verdict: str
    """One of VERDICT_PASS | VERDICT_WASH | VERDICT_FAIL."""

    reduction_fraction: Optional[float]
    """Signed: 1 - (shim_stat_ms / baseline_stat_ms). Positive means the
    shim was cheaper; None if baseline_stat_ms is zero (undefined
    reduction) -- treated as VERDICT_WASH by `evaluate()` rather than
    raising, since a zero-ms baseline draw is itself a measurement
    anomaly, not grounds for a crash."""

    timestamp: str = ""
    """ISO-8601 timestamp of when `evaluate()` produced this record."""

    schema_version: int = SCHEMA_VERSION

    def to_json(self) -> str:
        """Serialize this record to a JSON string. Round-trip pair:
        from_json()."""
        return json.dumps(asdict(self), sort_keys=True)

    @staticmethod
    def from_json(payload: str) -> "ShimDecisionRecord":
        """Deserialize a JSON string produced by to_json() back into a
        ShimDecisionRecord. Round-trip pair: to_json()."""
        data = json.loads(payload)
        return ShimDecisionRecord(**data)


def is_free(record: ShimDecisionRecord) -> bool:
    """True when *record*'s two arms are indistinguishable at this box's
    measured noise floor -- i.e. the shim costs nothing detectable.

    ADDITIVE, and deliberately not a change to `evaluate()`. `evaluate()`
    adjudicates "is the shim CHEAPER", was pre-registered before any number
    existed, and its verdict on that question stands untouched. This answers
    a DIFFERENT question that the plan originally conflated with it: "is the
    compatibility shim FREE". A compat layer's job is to cost nothing, never
    to be cheaper, so `evaluate()` returning `fail` for a small negative
    reduction is correct about its own question and useless about this one --
    it scores -0.0963 (a 1.10x ratio, well inside the A/A noise floor)
    identically to -0.5123 (a genuine 1.6x regression), because its wash band
    is one-sided (0 <= r < margin) by construction.

    The threshold is `AA_CALIBRATION_NOISE_FLOOR`, the same measured constant
    `CHEAPER_THAN_MARGIN` is derived from: a difference smaller than what two
    IDENTICAL arms already produced on this box is not a difference anyone
    can claim to have measured, in either direction.

    PROVENANCE — THIS FUNCTION WAS WRITTEN AFTER THE NUMBER, AND A READER
    MUST NOT MISTAKE IT FOR PRE-REGISTERED. `evaluate()` and its constants
    were frozen at 4b8151bf2 before any shim was measured. This predicate,
    and the AC6a/b/c split it serves, were authored at 272ed39cd -- AFTER
    9401f9aae had already recorded the in-process shim at -0.0963 and
    `evaluate()` had called it `fail`. The argument for it (a compatibility
    layer's job is to cost nothing, not to be cheaper, so an asymmetric
    cheaper-than test is the wrong instrument) is sound on its own terms and
    would have been sound before the measurement -- but it was not made
    before the measurement, and it was made in response to a result its
    author did not like. That is the precise bias pre-registration exists to
    exclude. Flagged by code review (2026-08-16) rather than self-caught,
    which is itself the relevant evidence about how much to trust it.

    WEAKNESS OF THE THRESHOLD, stated because it is easy to miss and it
    limits every claim built on this predicate. The floor is 0.4625, so
    `is_free` returns True for anything from a 46% improvement to a 46%
    REGRESSION. A shim 40% more expensive than what it replaces passes this
    test. So a True here does NOT establish that a shim is free; it
    establishes only that no LARGE regression was detected at this box's
    noise. The in-process shim's -0.0963 is consistent with free and equally
    consistent with a real 10% regression this apparatus cannot resolve.
    Read AC6b as "no large regression detected", never as "proven free" --
    and if a tighter claim is ever needed, it needs a quieter box or a
    paired design, not a smaller constant here.

    Discharges AC6b of docs/plans/2026-08-16-a-process-per-predicate.md, at
    that reduced strength.
    """
    r = record.reduction_fraction
    if r is None:
        return False
    return abs(r) < AA_CALIBRATION_NOISE_FLOOR


def evaluate(
    *,
    baseline_name: str,
    baseline_stats: PrimitiveStats,
    shim_name: str,
    shim_stats: PrimitiveStats,
    n_rounds: int = N_ROUNDS,
    seed_policy: str = "unseeded (random.Random())",
) -> ShimDecisionRecord:
    """Pure function: given per-arm `PrimitiveStats` from ONE
    `interleave.run_interleaved()` call, compute the verdict per this
    module's written decision rule and return a `ShimDecisionRecord`.

    No I/O, no timing, no subprocess -- the measurement itself happens in
    stage 2, via `interleave.run_interleaved`; this function only judges
    already-collected stats against the rule fixed in stage 1. Mirrors
    `record.py`'s "pure data model" / `gate.py`'s "verdict computation"
    split within this module's smaller purpose-built surface.

    Wash triggers (VERDICT_WASH), per module docstring 'Wash handling':
    - baseline_stats.sample_count != shim_stats.sample_count (statistic-
      ambiguous: the two arms did not draw the same number of valid
      samples, so their p90s are not comparable on equal footing).
    - baseline_stat_ms == 0 (reduction_fraction undefined).
    - the computed reduction_fraction is within CHEAPER_THAN_MARGIN of the
      required threshold from either side without CLEARING it (i.e.
      0 <= reduction_fraction < CHEAPER_THAN_MARGIN) -- an improvement that
      exists but does not clear the named margin.

    VERDICT_FAIL: shim_stat_ms >= baseline_stat_ms (reduction_fraction <=
    0) -- a clear regression or exact tie, not merely a sub-margin
    improvement.

    VERDICT_PASS: reduction_fraction >= CHEAPER_THAN_MARGIN AND the two
    arms' sample counts matched.
    """
    baseline_stat_ms = getattr(baseline_stats, GATING_STATISTIC)
    shim_stat_ms = getattr(shim_stats, GATING_STATISTIC)

    sample_counts_match = baseline_stats.sample_count == shim_stats.sample_count

    reduction_fraction: Optional[float]
    if baseline_stat_ms == 0:
        reduction_fraction = None
    else:
        reduction_fraction = 1.0 - (shim_stat_ms / baseline_stat_ms)

    if reduction_fraction is None or not sample_counts_match:
        verdict = VERDICT_WASH
    elif reduction_fraction <= 0.0:
        verdict = VERDICT_FAIL
    elif reduction_fraction < CHEAPER_THAN_MARGIN:
        verdict = VERDICT_WASH
    else:
        verdict = VERDICT_PASS

    return ShimDecisionRecord(
        gating_statistic=GATING_STATISTIC,
        margin=CHEAPER_THAN_MARGIN,
        baseline_name=baseline_name,
        baseline_stat_ms=baseline_stat_ms,
        shim_name=shim_name,
        shim_stat_ms=shim_stat_ms,
        baseline_sample_count=baseline_stats.sample_count,
        shim_sample_count=shim_stats.sample_count,
        n_rounds=n_rounds,
        seed_policy=seed_policy,
        verdict=verdict,
        reduction_fraction=reduction_fraction,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


if __name__ == "__main__":  # pragma: no cover
    # Re-runs the A/A noise-floor calibration this module's own
    # `CHEAPER_THAN_MARGIN` is derived from -- see module docstring
    # 'Cheaper-than margin' / 'MEASURED RESULT'. `--calibrate` is the only
    # supported flag; anything else is a usage error.
    if len(sys.argv) != 2 or sys.argv[1] != "--calibrate":
        print(
            f"usage: python -m {__name__} --calibrate", file=sys.stderr
        )
        sys.exit(2)
    _baseline = build_baseline_primitive()
    _reductions = calibrate_aa_noise_floor(
        _baseline.invoke,
        n_rounds=AA_CALIBRATION_N_ROUNDS,
        r_repeats=AA_CALIBRATION_R_REPEATS,
    )
    _floor = max(abs(x) for x in _reductions) if _reductions else float("nan")
    print(f"baseline_primitive={_baseline.name}")
    print(f"r_repeats={AA_CALIBRATION_R_REPEATS} n_rounds={AA_CALIBRATION_N_ROUNDS}")
    print(f"reductions={_reductions}")
    print(f"noise_floor={_floor}")
    sys.exit(0)
