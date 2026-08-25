"""coordinator_core.op_census.timing — handler-elapsed and invocation-tax axes,
with the three-state rule.

Purpose: the two timing axes DR-344 names — "process time per op" and
"invocation tax" (time to reach the warm engine) — reported per op as one of
exactly three states: `over_bar`, `under_bar`, `no_data`. Telemetry covers 94
of 273 ops (35%, measured 2026-08-21) — the rest MUST report `no_data`, never
silently pass. See plan `docs/plans/2026-08-21-the-census-that-cannot-miss-an-op.md`
§ "The three-state rule".

This axis is aggregated from real `op_latency.jsonl` telemetry (per-op
p50/max over `kind="complete"` rows, 60.1ms measured over 28,117 rows).
`ipc.dispatch_message`'s `perf_start`/`perf_counter` site is SCOPED to
`_dispatch_message_impl` — it times the server-side handler and nothing
else — but `perf_counter` is a WALL CLOCK, and a narrow scope does not
convert wall clock into process time. What this axis reports is handler-
scoped elapsed wall time, which under § Load norm (~50 concurrent
sessions) accrues time spent waiting for CPU that neighbouring sessions
are using. See the negative spec below for what that costs.

Invocation tax is NOT a field `op_latency.jsonl` carries per op — the sink
measures server-side dispatch time only, never the client-side cost of
reaching the door (§ spike verdict `probe_tax.py`). It is a single measured
floor (interpreter start + import cost), live-probed via a child process
reporting its OWN `time.process_time()`, and applied uniformly to every op
asked about — see `measure_invocation_tax_ms`.

Routed denominator (DR-332 § Item 2): a null/missing `route` field means the
row is UNOBSERVABLE (written before the `route` field existed, or by a path
that never resolved one), not cold. Using null-route rows as the denominator
understates a share by roughly two orders of magnitude. Every aggregation in
this module filters to `routed_entries` first.

Negative-spec:
    - Never `os.times()` child fields for a tax measurement — always `0.0` on
      Windows, and a prior probe built on them reported PASS for a 421ms
      import. `measure_invocation_tax_ms` has the child report its own
      `time.process_time()` instead (see `test_no_data_is_not_a_pass.py`'s
      regression guard).
    - CORRECTED 2026-08-21, recorded rather than swapped out. This block
      previously read: "Never wall clock. `handler_elapsed_by_op` reads
      `elapsed_ms`, which is itself server-side process time by
      construction of its writer (`ipc.dispatch_message`)." That is FALSE.
      `ipc.dispatch_message` computes `elapsed_ms` from `_time.perf_counter()`
      — wall clock. The true half of the claim is that the site is scoped to
      `_dispatch_message_impl`; the false half is reading that scope as a
      clock type. Both halves appeared, so the module asserted the
      falsehood twice (see § Process time above, corrected in the same pass).
      It is recorded here because this is the SECOND wrong-value-producer in
      this module's history — the `os.times()` note directly above is the
      first — and the pattern is that the negative spec written to prevent
      the class asserted a falsehood of the same class, two lines below the
      note. A reader checking this axis reads that line and stops; one did.
      Found by a peer audit (`project-makima-05`) during the C-review of the
      workstream that shipped it, not by this module's own tests.
    - CONSEQUENCE, unresolved: under § Load norm an `over_bar` on this axis
      can reflect peer load rather than the op's own cost, and the census
      cannot tell the two apart. Per-op CPU attribution under async
      concurrency is a real design question and is NOT a clock substitution:
      `time.process_time()` in the server measures the WHOLE process's CPU,
      so a delta taken across an `await` attributes concurrent ops' work to
      whichever op is being timed, and `thread_time` does not separate async
      ops sharing a thread. Do not "fix" this by swapping the clock.
      DR-344's load-independence requirement binds the CENSUS's own
      self-assertion (see C6); what it does NOT do is license naming this
      axis after a clock it does not use.
    - RENAMED 2026-08-21, closing the line directly above: the axis, its
      function, and its emitted key are `handler_elapsed`, not
      `process_time`. Two reasons, and the second is the load-bearing one.
      First, the old name asserted a clock this axis has never read.
      Second, `process_time` was ALREADY TAKEN by real data:
      `ipc.record_op_process_time` writes genuine per-op CPU samples to this
      same sink under row `kind: "process_time"`, with a
      `measurement_scope` discriminator separating per-op from process-wide.
      One name over two different quantities in one sink is the exact
      unit-mixing hazard that row was created to avoid, and it had been
      reintroduced one level up, in the artifact a consumer reads.
      NOT substituted, and this was measured rather than assumed: those true
      CPU rows cover 13 of 109 ops in the live sink today (173 rows, 121 of
      them `per_op_process`), against 29,479 `complete` rows. Repointing this
      axis at them now would blank 88% of the population to `no_data`. The
      substitution becomes correct when coverage arrives, and at that point
      it is a NEW axis alongside this one, keyed `process_time` honestly —
      not a redefinition of `handler_elapsed` in place, for the same reason
      `process_ms` was never a redefinition of `elapsed_ms`.
    - CORRECTED 2026-08-23, recorded rather than swapped out. This is the
      module's THIRD wrong-value-producer on the invocation-tax axis — the
      `os.times()` note and the wall-clock/`process_time` naming pair above
      are the first two — and the FIRST to reach the census's EMITTED
      output rather than only its docstring; the first two were caught by a
      peer audit before shipping, this one shipped and reported "every op
      breaches" for an unknown span. `measure_invocation_tax_ms` spawned
      `[sys.executable, "-S", "-c", "import coordinator_core.ops"]` — a
      bare interpreter with `site` disabled, a shape nothing in production
      runs. Measured on this box, this pass, n=5: mean 431.25ms / 598
      modules against the 50ms bar (the bug row that filed this fix cites a
      close but distinct n=3 sample, 395.8ms / 594 modules, taken in the
      same shape — both over the bar by roughly 8x). `invocation_tax_dispositions`
      applies that ONE measured floor uniformly to every op (by design —
      see the top of this docstring), so it stamped `OVER_BAR` on the
      entire population, always, and `cleared_ops` (which requires
      `UNDER_BAR` on every axis) returned an empty set regardless of how
      good `handler_elapsed` looked, for as long as this shipped.

      DR-344 constraint 3 is "the cost to *get to* the warm engine must be
      under 50ms." Nothing in production starts a bare interpreter and
      imports `coordinator_core.ops` cold — the two real shapes are the
      warm engine itself (0.0ms, package already resident) and the
      one-shot/trampoline cold path, where op registration is lazy
      unconditionally, so only the package's own `__init__` runs rather
      than all ~55 op modules. The probe now measures THAT shape: `-S`
      dropped (nothing in production disables `site`), and the child's
      `env=` built here rather than by routing through
      `coordinator/bin/lib/cc_invoke.py::child_env()`, whose job is
      stripping that exact variable from children spawned along the
      trampoline path. Measured on this box, this pass, under the new
      shape: mean 6.25ms / 99 modules over 5 iterations (plain `-c`,
      `canary_op_imported: False` confirmed on every sample) — under the
      50ms bar, inverting every op's disposition on this axis from breach
      to pass. See
      `measure_invocation_tax_ms`'s docstring for the full shape argument
      and the two traps (the `sys`-attribute boundary, the `child_env()`
      stripper) a naive rewrite hits.

      Also added: a uniformity guard at the `emit_dispositions` boundary
      (`UniformInvocationTaxError`) that refuses to emit a cleared set when
      the tax axis reports `OVER_BAR` for every op — the exact shape that
      hid this incident. Uniformity itself is not the guarded-against
      signature (this axis is a single floor broadcast to every op BY
      DESIGN, so it is always uniform whenever it has data at all);
      uniform `OVER_BAR` specifically is, because it is indistinguishable
      from a broken measurement from inside this module and it is what
      silently emptied `cleared_ops` here.
    - `no_data` never collapses into `under_bar`. `cleared_ops` dispatches
      the three `Disposition` states exhaustively with an explicit
      `RuntimeError` else-branch — no default branch — so a fourth state
      added to the enum later fails loudly here rather than falling through
      to a pass. This guard sits at the EMITTED-disposition boundary
      (`emit_dispositions`), not only the in-memory enum, per staff-eng
      Finding 7: an enum-only test would not have caught the os.times()
      incident, which was a wrong-value PRODUCER, not a collapsed enum.
    - `no_data` carries a reason (`NoDataReason`): `never_observed` (no
      telemetry for this op anywhere) vs `not_in_current_generation` (known
      to exist in a rotated generation this module does not read — see
      plan § Out of scope). Cheap at emit time via the optional
      `rotated_generation_ops` parameter; the rotated-generation aggregation
      itself stays deferred.
    - This module produces evidence (per-op axis values and dispositions),
      never a verdict about what to do with them — no kill/purge decision
      lives here (plan § Anti-scope: "Do not kill anything").

Spec backlink: state/dispatch-briefs/2026-08-21-the-census-that-cannot-miss-an-op/C4.md
               docs/plans/2026-08-21-the-census-that-cannot-miss-an-op.md
               docs/decisions/DR-332-the-directive-cost-unit-half-is-deferred-and-the-warmth-gate-is-refused.md
               docs/decisions/DR-344-the-brightline-process-budget-for-makima.md
"""

from __future__ import annotations

import dataclasses
import enum
import json
import os
import statistics
import subprocess
import sys
from typing import Dict, Iterable, List, Optional

from coordinator_core.telemetry.op_latency import EXECUTION_ROUTES

__all__ = [
    "Disposition",
    "NoDataReason",
    "AxisResult",
    "PROCESS_TIME_BAR_MS",
    "INVOCATION_TAX_BAR_MS",
    "UniformInvocationTaxError",
    "_raise_if_tax_uniformly_over_bar",
    "routed_entries",
    "handler_elapsed_by_op",
    "measure_invocation_tax_ms",
    "invocation_tax_dispositions",
    "cleared_ops",
    "emit_dispositions",
]


class Disposition(enum.Enum):
    """Exactly three states. Never a fourth — see module docstring."""

    OVER_BAR = "over_bar"
    UNDER_BAR = "under_bar"
    NO_DATA = "no_data"


class NoDataReason(enum.Enum):
    """Why an op reports `no_data` — staff-eng Finding 8.

    `NOT_ESTABLISHED_UNDER_LOAD` (added alongside the module docstring's
    2026-08-21 wall-clock correction): `handler_elapsed_by_op` reads
    `elapsed_ms`, which is wall clock, not process time (see module
    docstring). Under this repo's load norm, wall time accruing past
    `PROCESS_TIME_BAR_MS` can reflect a busy neighbour, not the op's own
    cost — a breach reading this axis cannot support is reported here as
    "not established", never as `over_bar`. `under_bar` stays sound and
    unaffected: elapsed wall time under the bar means the op's own CPU time
    is certainly under it too."""

    NEVER_OBSERVED = "never_observed"
    NOT_IN_CURRENT_GENERATION = "not_in_current_generation"
    NOT_ESTABLISHED_UNDER_LOAD = "not_established_under_load"


@dataclasses.dataclass(frozen=True)
class AxisResult:
    """One op's disposition on one axis.

    `no_data_reason` is required if and only if `disposition` is `NO_DATA` —
    enforced in `__post_init__` so a `NO_DATA` result can never be
    constructed without a reason, and a non-`NO_DATA` result can never carry
    a stray one.
    """

    disposition: Disposition
    p50_ms: Optional[float] = None
    max_ms: Optional[float] = None
    sample_count: int = 0
    no_data_reason: Optional[NoDataReason] = None

    def __post_init__(self) -> None:
        if self.disposition is Disposition.NO_DATA and self.no_data_reason is None:
            raise ValueError("NO_DATA disposition requires a no_data_reason")
        if self.disposition is not Disposition.NO_DATA and self.no_data_reason is not None:
            raise ValueError("no_data_reason is only valid for a NO_DATA disposition")


#: DR-344 § the kill bar: >=500ms is "not holding" (whether the 500ms-1s
#: refactor-or-kill zone or the >1s kill zone) — the three-state rule
#: compresses that two-tier breach shape to a single `over_bar`, verdicts
#: about which zone stay out of this evidence module (plan § Anti-scope).
PROCESS_TIME_BAR_MS: float = 500.0

#: DR-344 constraint 3: "the cost to *get to* the warm engine must be under
#: 50ms."
INVOCATION_TAX_BAR_MS: float = 50.0


def routed_entries(entries: Iterable[dict]) -> List[dict]:
    """Entries whose `route` is a recognised execution route.

    A null/missing route is UNOBSERVABLE, not cold (DR-332 § Item 2) — using
    it as the denominator understates a share by roughly two orders of
    magnitude. Excluded here, never zero-filled, so every caller downstream
    computes coverage/shares against the routed population only.
    """
    return [e for e in entries if isinstance(e, dict) and e.get("route") in EXECUTION_ROUTES]


def handler_elapsed_by_op(
    entries: Iterable[dict],
    ops: Iterable[str],
    *,
    bar_ms: float = PROCESS_TIME_BAR_MS,
    rotated_generation_ops: Optional[Iterable[str]] = None,
) -> Dict[str, AxisResult]:
    """Per-op p50/max process-time disposition over ROUTED `complete` rows.

    `ops` is the full population to report over (e.g. every registered op) —
    an op absent from routed telemetry gets `NO_DATA` rather than being
    silently omitted, which is the exact failure the three-state rule exists
    to forbid. `over_bar` fires if EITHER the p50 or the max breaches
    `bar_ms` — a hot tail matters even when the median looks fine.

    `rotated_generation_ops`, if given, names ops known (cheaply, without
    this module aggregating the rotated generations itself — see § Out of
    scope) to have appeared outside the current generation; an absent op in
    that set reports `NOT_IN_CURRENT_GENERATION` instead of
    `NEVER_OBSERVED`.

    `elapsed_ms` is wall clock (module docstring), so a breach here is
    reported as `NO_DATA`/`NOT_ESTABLISHED_UNDER_LOAD`, never `OVER_BAR` —
    peer load alone can produce it, and this axis cannot tell the two
    apart. `UNDER_BAR` stays sound: elapsed wall time under `bar_ms` means
    the op's own process time is certainly under it too.
    """
    rotated = set(rotated_generation_ops) if rotated_generation_ops is not None else frozenset()

    samples: Dict[str, List[float]] = {}
    for entry in routed_entries(entries):
        kind = entry.get("kind") or "complete"
        if kind != "complete":
            continue
        op = entry.get("op")
        elapsed = entry.get("elapsed_ms")
        if not isinstance(op, str) or not isinstance(elapsed, (int, float)):
            continue
        samples.setdefault(op, []).append(float(elapsed))

    results: Dict[str, AxisResult] = {}
    for op in ops:
        values = samples.get(op)
        if not values:
            reason = (
                NoDataReason.NOT_IN_CURRENT_GENERATION
                if op in rotated
                else NoDataReason.NEVER_OBSERVED
            )
            results[op] = AxisResult(disposition=Disposition.NO_DATA, no_data_reason=reason)
            continue

        p50 = statistics.median(values)
        mx = max(values)
        if p50 >= bar_ms or mx >= bar_ms:
            # Wall clock cannot support an OVER_BAR verdict (module
            # docstring) -- report as not-established rather than a breach
            # this axis is not sound to claim.
            results[op] = AxisResult(
                disposition=Disposition.NO_DATA,
                no_data_reason=NoDataReason.NOT_ESTABLISHED_UNDER_LOAD,
            )
        else:
            results[op] = AxisResult(
                disposition=Disposition.UNDER_BAR,
                p50_ms=p50,
                max_ms=mx,
                sample_count=len(values),
            )
    return results


#: Canary op module. `coordinator_core.ops` never imports this module as
#: part of its own package init UNLESS an eager 55-module sweep ran — so
#: its presence in the child's `sys.modules` after the probe import is proof
#: registration was NOT lazy, distinguishing a genuine trampoline-cold-path
#: sample from a silently-reverted bare-interpreter one.
_TAX_PROBE_CANARY_MODULE = "coordinator_core.ops.ping"

#: The measurement script run by `measure_invocation_tax_ms`. Reports the
#: CHILD's own `time.process_time()` around importing the op registry, plus
#: `module_count` and `canary_op_imported` so the caller can PROVE the child
#: actually reached the shape it asked for rather than assuming it (see
#: `measure_invocation_tax_ms`'s arming check). Deliberately never
#: `os.times()` (child fields are always `0.0` on Windows — see module
#: docstring).
_TAX_PROBE_SCRIPT = (
    "import json, sys, time\n"
    "t0 = time.process_time()\n"
    "import coordinator_core.ops\n"
    "t1 = time.process_time()\n"
    "sys.stdout.write(json.dumps({\n"
    "    'process_time_ms': (t1 - t0) * 1000.0,\n"
    "    'module_count': len(sys.modules),\n"
    f"    'canary_op_imported': {_TAX_PROBE_CANARY_MODULE!r} in sys.modules,\n"
    "}))\n"
)


class UniformInvocationTaxError(RuntimeError):
    """Raised when the invocation-tax axis reports `OVER_BAR` for every op.

    This axis is one measured floor broadcast identically to every op
    (module docstring), so `OVER_BAR`-uniform is the exact signature a
    broken measurement leaves: it is indistinguishable, from inside this
    module, from "the fleet is genuinely uniform" — see
    `emit_dispositions`'s uniformity guard. Raised there rather than
    swallowed, because a permanently-OVER_BAR axis silently empties
    `cleared_ops` (staff-eng Finding 7 / this axis's THIRD wrong-value
    incident, module docstring) and a guard that only lived in-memory would
    not have caught that emitted output was wrong.
    """


def _raise_if_tax_uniformly_over_bar(invocation_tax: Dict[str, AxisResult]) -> None:
    """Raise `UniformInvocationTaxError` iff every op with an invocation-tax
    result reports `OVER_BAR`.

    Single source of truth for the discriminator both emission boundaries
    need — `emit_dispositions` (this module) and
    `coordinator_core/ops/op_census_report.py::_four_axis_report` (the real
    emitted boundary `op_census.report` clients read). Before this factor-
    out (review finding #6, slice C review of commit c7cb4a565) the same
    condition, exception type, and near-identical message were hand-copied
    verbatim into both call sites — nothing forced them to stay in sync, so
    an edit to one (e.g. tightening the guard, or changing what counts as
    "uniform") could silently leave the other, including the live one,
    unchanged. That is exactly the "one cause, both halves wrong" pattern
    this axis's own 2026-08-23 CORRECTED negative-spec block describes for
    the original incident. See `UniformInvocationTaxError`'s docstring for
    why uniform `OVER_BAR` specifically is the guarded-against signature.
    """
    tax_dispositions = {r.disposition for r in invocation_tax.values()}
    if tax_dispositions == {Disposition.OVER_BAR}:
        raise UniformInvocationTaxError(
            f"invocation_tax is OVER_BAR for all {len(invocation_tax)} op(s) it "
            "covers -- this is the signature of a broken measurement (module "
            "docstring's 2026-08-23 CORRECTED block), not evidence that the "
            "entire fleet breaches DR-344 constraint 3. Refusing to emit a "
            "cleared set built on this axis."
        )


def measure_invocation_tax_ms(*, iterations: int = 5, timeout_secs: float = 30.0) -> float:
    """Live-measure the cost of reaching a warm engine via the TRAMPOLINE
    COLD PATH — DR-344 constraint 3 ("the cost to *get to* the warm engine
    must be under 50ms").

    Shape, and why (module's THIRD wrong-value incident on this axis, see
    docstring's 2026-08-23 CORRECTED block): production never starts a bare
    interpreter and imports `coordinator_core.ops` cold. The two real
    shapes are the warm engine itself (0.0ms — the package is already
    resident, nothing to measure) and the one-shot/trampoline cold path,
    where op registration is lazy unconditionally, so only the package's
    own `__init__` runs, not all ~55 op modules. This probe measures that
    second shape.

    Laziness needs no arming and no channel: `ops/__init__.py` registers
    nothing at import, so a child inherits the measured shape by default.
    The canary above proves it per sample rather than assuming it. This
    function builds its own `env=` rather than routing through
    `coordinator/bin/lib/cc_invoke.py::child_env()` — simpler, and
    independent of that function's own internal state, which is the actual
    reason to prefer it here.

    `-S` is dropped: nothing in production disables `site`, and doing so
    was most of the old probe's inflated module count.

    Laziness is PROVEN per sample, never assumed: each child reports whether
    `_TAX_PROBE_CANARY_MODULE` ended up in its own `sys.modules`, which only
    happens if an eager sweep ran. A canary hit raises `RuntimeError`
    immediately — an eager sample would quietly average a bare-interpreter
    cost back into this axis, the same failure class this rewrite exists to
    close.

    Multiple `iterations` are averaged because the ~15.6ms Windows
    process-time quantum makes a single sample pure noise near the 50ms
    bar. Returns `float("nan")` if every child failed to report a usable
    value (never raises for THAT case) — callers must treat NaN as "no
    measurement", not as a disposition input; `invocation_tax_dispositions`
    does this.
    """
    samples: List[float] = []
    child_env = dict(os.environ)
    for _ in range(iterations):
        try:
            proc = subprocess.run(
                [sys.executable, "-c", _TAX_PROBE_SCRIPT],
                capture_output=True,
                text=True,
                timeout=timeout_secs,
                env=child_env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        try:
            payload = json.loads(proc.stdout)
            if payload.get("canary_op_imported"):
                raise RuntimeError(
                    "measure_invocation_tax_ms: child registered ops eagerly "
                    f"({_TAX_PROBE_CANARY_MODULE} was imported anyway). "
                    "Registration is unconditionally lazy; an eager child "
                    "silently reverts the measurement to the bare-interpreter "
                    "shape this axis was rewritten to stop measuring (see "
                    "module docstring's 2026-08-23 CORRECTED block)."
                )
            samples.append(float(payload["process_time_ms"]))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue

    if not samples:
        return float("nan")
    return statistics.mean(samples)


def invocation_tax_dispositions(
    ops: Iterable[str],
    *,
    measured_tax_ms: Optional[float] = None,
    bar_ms: float = INVOCATION_TAX_BAR_MS,
) -> Dict[str, AxisResult]:
    """Per-op invocation-tax disposition from a single measured floor.

    Invocation tax is not a per-op telemetry field (see module docstring) —
    it is one measured value applied uniformly to every op named in `ops`.
    `measured_tax_ms` should come from `measure_invocation_tax_ms`
    (injectable here so tests never pay a real subprocess spawn). `None` or
    NaN reports `NO_DATA`/`never_observed` for every op rather than
    fabricating a number.
    """
    if measured_tax_ms is None or measured_tax_ms != measured_tax_ms:  # NaN check, no math import
        return {
            op: AxisResult(disposition=Disposition.NO_DATA, no_data_reason=NoDataReason.NEVER_OBSERVED)
            for op in ops
        }

    disposition = Disposition.OVER_BAR if measured_tax_ms >= bar_ms else Disposition.UNDER_BAR
    return {
        op: AxisResult(disposition=disposition, max_ms=measured_tax_ms, sample_count=1)
        for op in ops
    }


def cleared_ops(
    handler_elapsed: Dict[str, AxisResult],
    invocation_tax: Dict[str, AxisResult],
) -> set:
    """Ops CLEARED across both axes — every axis `UNDER_BAR`, none `NO_DATA`.

    Dispatches the three `Disposition` states EXHAUSTIVELY, with no default
    branch: an unrecognised disposition value raises `RuntimeError`
    immediately rather than falling through to a pass. This is the guard
    staff-eng Finding 7 requires sitting at the EMITTED-disposition
    boundary, not only the in-memory enum — an enum-only test would not
    have caught the os.times() incident, which was a wrong-value PRODUCER,
    not a collapsed enum.

    An op missing from either axis map is excluded from `cleared`, never
    defaulted into it — the three-state rule's whole point is that absence
    of data must never read as a pass.
    """
    cleared: set = set()
    for op in handler_elapsed.keys() | invocation_tax.keys():
        pt = handler_elapsed.get(op)
        tax = invocation_tax.get(op)
        if pt is None or tax is None:
            continue

        op_cleared = True
        for result in (pt, tax):
            if result.disposition is Disposition.OVER_BAR:
                op_cleared = False
            elif result.disposition is Disposition.NO_DATA:
                op_cleared = False
            elif result.disposition is Disposition.UNDER_BAR:
                pass
            else:
                raise RuntimeError(f"unhandled Disposition {result.disposition!r} in cleared_ops")

        if op_cleared:
            cleared.add(op)
    return cleared


def _serialize_axis(result: Optional[AxisResult]) -> dict:
    if result is None:
        return {
            "disposition": Disposition.NO_DATA.value,
            "no_data_reason": NoDataReason.NEVER_OBSERVED.value,
        }
    payload: dict = {"disposition": result.disposition.value}
    if result.disposition is Disposition.NO_DATA:
        payload["no_data_reason"] = (
            result.no_data_reason.value if result.no_data_reason is not None else None
        )
    else:
        payload["p50_ms"] = result.p50_ms
        payload["max_ms"] = result.max_ms
        payload["sample_count"] = result.sample_count
    return payload


def emit_dispositions(
    handler_elapsed: Dict[str, AxisResult],
    invocation_tax: Dict[str, AxisResult],
) -> dict:
    """Serialize per-op axis dispositions to the machine-readable shape C6 assembles.

    Every op named by either axis map appears with BOTH axes — an op absent
    from the output would silently read as "no opinion" to a downstream
    consumer, the exact failure the three-state rule forbids. `cleared`
    names the ops `cleared_ops` marks passing; `test_no_data_is_not_a_pass.py`
    asserts an op carrying `no_data` on any axis never appears there,
    checked against THIS emitted shape, not only the in-memory `Disposition`
    enum.

    Uniformity guard, at THIS emitted-disposition boundary rather than only
    in-memory (staff-eng Finding 7 — an enum-only check would not have
    caught the incident this guards against, which was a wrong-value
    PRODUCER, not a collapsed enum): the invocation-tax axis is one measured
    floor broadcast identically to every op (module docstring), so a
    genuinely broken measurement and a genuinely uniform fleet look
    identical from inside this module. Raises `UniformInvocationTaxError`
    if every op with an invocation-tax result reports `OVER_BAR` — the
    specific shape that hid this axis's third wrong-value incident: it
    silently made `cleared_ops` permanently empty while reporting "every op
    breaches" as though it were a real finding. Uniform `UNDER_BAR` is not
    flagged — it is what a healthy, passing measurement looks like by this
    axis's own single-floor design, and is not the failure signature this
    guard exists for.

    Discriminator itself lives in `_raise_if_tax_uniformly_over_bar`, shared
    with `op_census_report.py::_four_axis_report` (review finding #6, slice
    C) so the two emission boundaries cannot drift apart.
    """
    ops = sorted(handler_elapsed.keys() | invocation_tax.keys())
    _raise_if_tax_uniformly_over_bar(invocation_tax)
    passing = cleared_ops(handler_elapsed, invocation_tax)
    return {
        "ops": {
            op: {
                "handler_elapsed": _serialize_axis(handler_elapsed.get(op)),
                "invocation_tax": _serialize_axis(invocation_tax.get(op)),
            }
            for op in ops
        },
        "cleared": sorted(passing),
    }
