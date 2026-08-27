"""
coordinator_core.session.tests.test_touch_record_perf — the measurement
`touch_record.append_event` never had (C8, docs/plans/2026-08-25-the-
touched-files-record-gets-a-designed-shape.md).

Half 1 of C8's brief: prove the per-append cost curve is FLAT across 0, 1k,
and 10k prior events on the same sink, plus a full read at a realistic
length -- never a single-threshold assertion, because a threshold passes on
an empty file and that is exactly how the killed op's own O(D^2) dedup-read
defect stayed hidden behind an 11.1ms median (see `touch_record.py`'s own
module docstring, AC17 section).

MEASUREMENT DISCIPLINE (non-negotiable, repeated here because it governs
every assertion in this file):
    - Every figure comes from `benchmarks.process_time.batched_process_time_ms`
      and reports `procs_per_call`. Process time and spawn count only --
      NEVER wall clock (this box runs 50-70 concurrent sessions; wall clock
      measures peer load, not this code -- CLAUDE.md § Load norm).
    - A bare `time.process_time()` figure never backs an assertion in this
      file. It is used exactly once, read-only, to fetch
      `time.get_clock_info('process_time').resolution` for the tick-guard
      below -- never to time an operation itself.

TRAP 1 (hit live while drafting C8's own dispatch brief; do not repeat):
    Windows' job-object process-time accounting quantises to a ~15.625ms
    scheduler tick (`benchmarks/process_time.py`'s own module docstring,
    trap 2) -- a coarser floor than `time.get_clock_info('process_time')
    .resolution` (a CPython-level clock-info figure, and a DIFFERENT clock
    from the job-object accounting `batched_process_time_ms` actually reads
    on Windows) ever admits on this box. A per-append figure divided out of
    a few hundred iterations lands on exact multiples of that tick and
    LOOKS like a clean rising curve when it is pure quantisation. Guarded
    here by `_TICK_MS` (the documented, not the theoretical, floor) and
    `_assert_well_above_tick`, which every measured total in this file
    passes through BEFORE its per-op division is trusted.

TRAP 2 (same session): the read-modify-write arm this module's own
    docstring already retired (the O(D^2) dedup read) is NEVER benchmarked
    here at a large prior-event count. It is quadratic by construction, it
    would occupy this box for minutes, and its cost is already established
    by the killed op's own history (11.1ms median hiding an unbounded
    curve). This file measures the NEW append-only shape only.

Every driver script below is written to a fresh scratch file and spawned via
`sys.executable`, matching the shape `benchmarks/tests/
test_commit_op_wallclock_budget.py::_write_driver` already established for
this repo's process-time instrument.
"""

from __future__ import annotations

import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks.process_time import batched_process_time_ms
from coordinator_core.session import touch_record

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

#: Documented Windows job-object scheduler tick (benchmarks/process_time.py
#: module docstring, trap 2) -- the floor `batched_process_time_ms` actually
#: reads through on this platform, which is coarser than
#: `time.get_clock_info('process_time').resolution` on this box (see this
#: module's own docstring, TRAP 1). The larger of the two is used as the
#: guard floor so the check never trusts a clock that lies optimistic.
_DOCUMENTED_WINDOWS_TICK_MS = 15.625

#: Per C8's own brief: "size the iteration count so the total measured
#: interval is at least two orders of magnitude above" the tick.
_MIN_ORDERS_OF_MAGNITUDE_ABOVE_TICK = 100

#: Number of `append_event` calls performed INSIDE one spawned driver, so
#: the interpreter-startup floor (paid once per invocation) is amortised
#: across enough real work that dividing it out does not itself become a
#: quantisation artifact (TRAP 1). Measured on this box: ~0.16ms/call at
#: this shape, so 20000 calls totals ~3.2s of process time per point --
#: comfortably (>100x) above the 15.625ms tick, and the reason this file is
#: `cadence`-tiered rather than fast-suite.
_APPENDS_PER_DRIVER = 20_000

#: `k` for the append-flatness legs. A single spawn per point already
#: amortises tick noise internally via `_APPENDS_PER_DRIVER`'s own loop;
#: `k=1` avoids the alternative (re-running the SAME driver argv `k` times
#: against a sink that keeps growing between reps, biasing later reps of
#: the SAME point upward for a reason that has nothing to do with the
#: point being measured).
_K_APPEND_FLATNESS = 1

#: The one bar. `SUSPENSION_BAR_MS` (2000ms) is which-to-switch-off-first,
#: never a target, and a figure in this file is never compared against it.
_BRIGHTLINE_MS = 500.0

#: `k` for the full-read leg, where re-running the identical read-only
#: driver against the same fixture carries no growth-between-reps hazard.
_K_FULL_READ = 5


def _tick_ms() -> float:
    """The guard floor for TRAP 1: the larger of this box's own
    `time.get_clock_info('process_time').resolution` (read once, for
    context -- never used to time an operation, per this module's own
    Measurement discipline section) and the documented Windows job-object
    scheduler tick `batched_process_time_ms` actually measures through."""
    reported_resolution_ms = time.get_clock_info("process_time").resolution * 1000.0
    return max(reported_resolution_ms, _DOCUMENTED_WINDOWS_TICK_MS)


def _assert_well_above_tick(total_process_time_ms: float, label: str) -> None:
    """TRAP 1's guard: refuse to trust a per-op figure divided out of a
    total that is not itself well above the scheduler tick. Asserts, does
    not silently clamp -- a figure that fails this check is not a flat
    curve, it is unmeasured, and the test must say so rather than pass."""
    tick = _tick_ms()
    floor = tick * _MIN_ORDERS_OF_MAGNITUDE_ABOVE_TICK
    assert total_process_time_ms >= floor, (
        f"{label}: total process time {total_process_time_ms}ms is not at "
        f"least {_MIN_ORDERS_OF_MAGNITUDE_ABOVE_TICK}x the {tick}ms tick "
        f"({floor}ms) -- a per-op figure divided out of this total would be "
        "pure quantisation, not a measurement (TRAP 1)."
    )


def _build_prior_events(sink: Path, count: int) -> None:
    """Write `count` prior events directly to `sink` as raw encoded bytes --
    NOT via `append_event`, so building the fixture (unmeasured setup, never
    inside a timed driver) pays a single `write_bytes` rather than `count`
    separate opens. This deliberately bypasses `_maybe_rotate`: the point of
    this fixture is prior RECORD LENGTH on one sink, matching how AC17's
    live rotation check will see it on the very next real `append_event`
    call (below the cap at count=0/1000, at or past it at count=10000 --
    exercising the rotate-on-first-append path for that point, exactly as
    a real oversized sink would)."""
    sink.parent.mkdir(parents=True, exist_ok=True)
    ts = 1_700_000_000.0
    lines = []
    for i in range(count):
        lines.append(
            touch_record.encode_line(
                session_id="sess-fixture",
                agent_id=None,
                verb=touch_record.VERB_TOUCH,
                path=f"prior_{i}.py",
                timestamp=ts + i,
            )
        )
    sink.write_bytes(b"".join(lines))


def _write_append_driver(driver_path: Path, sink: Path) -> None:
    """Writes a driver that performs `_APPENDS_PER_DRIVER` real
    `append_event` calls against `sink` (already seeded with the point's
    prior-event count by `_build_prior_events`) and exits 0. Every call
    goes through the real production path -- encode, `_maybe_rotate`'s O(1)
    stat, `atomic_append.append_line` -- exactly as a live `touch()` call
    would."""
    script = textwrap.dedent(
        f"""\
        import sys
        sys.path.insert(0, {str(Path(__file__).resolve().parents[3])!r})
        from coordinator_core.session import touch_record

        sink = {str(sink)!r}
        for i in range({_APPENDS_PER_DRIVER}):
            touch_record.append_event(
                sink,
                session_id="sess-driver",
                agent_id=None,
                verb=touch_record.VERB_TOUCH,
                path=f"driver_{{i}}.py",
                timestamp=1_800_000_000.0 + i,
            )
        print("ok")
        """
    )
    driver_path.write_text(script, encoding="utf-8")


def _measure_append_cost_ms(tmp_path: Path, prior_count: int, label: str) -> dict:
    sink = tmp_path / f"prior-{prior_count}" / "sess-driver" / "touch-record.jsonl"
    _build_prior_events(sink, prior_count)

    driver = tmp_path / f"driver-{prior_count}.py"
    _write_append_driver(driver, sink)

    result = batched_process_time_ms(
        [sys.executable, str(driver)], k=_K_APPEND_FLATNESS
    )
    assert result["rc"] == 0, f"{label} driver failed: {result!r}"
    _assert_well_above_tick(result["process_time_ms"], label)
    per_append_ms = result["process_time_ms"] / _APPENDS_PER_DRIVER
    return {
        "prior_count": prior_count,
        "total_process_time_ms": result["process_time_ms"],
        "procs_per_call": result["procs_per_call"],
        "per_append_ms": round(per_append_ms, 6),
    }


def test_append_cost_is_flat_across_prior_event_counts(tmp_path):
    """C8 Half 1: per-append cost at 0, 1k, and 10k prior events on the same
    sink. Asserts FLATNESS -- no growth trend across the three points --
    never a single threshold (module docstring: a threshold passes on an
    empty file and that is exactly how the killed op's O(D^2) dedup-read
    defect hid behind an 11.1ms median)."""
    points = [
        _measure_append_cost_ms(tmp_path, 0, "prior=0"),
        _measure_append_cost_ms(tmp_path, 1_000, "prior=1000"),
        _measure_append_cost_ms(tmp_path, 10_000, "prior=10000"),
    ]

    for point in points:
        assert point["procs_per_call"] == pytest.approx(1.0, abs=0.01), (
            f"a pure-Python append driver must spawn no subprocess of its "
            f"own: {point!r}"
        )

    per_append = [p["per_append_ms"] for p in points]
    detail = ", ".join(
        f"prior={p['prior_count']}: {p['per_append_ms']}ms/append "
        f"(total {p['total_process_time_ms']}ms over {_APPENDS_PER_DRIVER} calls)"
        for p in points
    )
    print(f"append-cost flatness curve: {detail}")

    baseline = per_append[0]
    # FLATNESS, not a threshold: every later point must stay within a
    # generous multiple of the empty-sink baseline -- a real O(D^2)-shaped
    # regression would blow past this by orders of magnitude at 10k prior
    # events (module docstring's own retired-defect comparison), while
    # ordinary run-to-run jitter on a shared, loaded box stays well inside
    # it.
    _GROWTH_TOLERANCE_MULTIPLE = 3.0
    for point in points[1:]:
        multiple = point["per_append_ms"] / baseline if baseline > 0 else float("inf")
        assert multiple <= _GROWTH_TOLERANCE_MULTIPLE, (
            f"per-append cost grew {multiple:.2f}x from prior=0 "
            f"({baseline}ms) to prior={point['prior_count']} "
            f"({point['per_append_ms']}ms) -- this is a growth trend, not "
            f"flat. {detail}"
        )


def _write_full_read_driver(driver_path: Path, sink: Path) -> None:
    script = textwrap.dedent(
        f"""\
        import sys
        sys.path.insert(0, {str(Path(__file__).resolve().parents[3])!r})
        from coordinator_core.session import touch_record

        projection = touch_record.project_live_claims({str(sink)!r})
        print(len(projection.claims), projection.degraded)
        """
    )
    driver_path.write_text(script, encoding="utf-8")


def _write_full_read_floor_driver(driver_path: Path) -> None:
    """The full-read driver with the `project_live_claims` call removed.

    NEGATIVE SPEC -- byte-identical to `_write_full_read_driver` above its
    read call, imports included, so subtracting it prices the read and only
    the read. A floor that imports less under-states itself and over-states
    the seam.
    """
    script = textwrap.dedent(
        f"""\
        import sys
        sys.path.insert(0, {str(Path(__file__).resolve().parents[3])!r})
        from coordinator_core.session import touch_record
        """
    )
    driver_path.write_text(script, encoding="utf-8")


#: THE DEEPEST CLAIMANT A SESSION CAN PLAUSIBLY WRITE, derived from the
#: live corpus measured 2026-08-27 (see docs/research/spike-verdicts/
#: 2026-08-27-corpus-c-is-wrong-on-both-axes-and-the-fingerprint-prize-
#: collapses-at-real-width.md): highest sustained per-session append rate
#: observed anywhere on the box, 132 events/hour, held for a full 24 hours.
#:
#: Per-claimant depth does NOT accumulate the way claimant COUNT does -- a
#: session's record stops growing when the session ends -- so the bound
#: here is session lifetime x append rate, never a calendar projection.
#: Measured reality for comparison: median 5 events, max 169.
_PEAK_APPEND_RATE_PER_HOUR = 132
_MAX_PLAUSIBLE_SESSION_HOURS = 24
_DEEPEST_PLAUSIBLE_CLAIMANT = _PEAK_APPEND_RATE_PER_HOUR * _MAX_PLAUSIBLE_SESSION_HOURS


def test_full_read_at_realistic_length_is_under_the_bar(tmp_path):
    """C8 Half 1's second half: a full `project_live_claims` read at the
    deepest single claimant a session can plausibly write -- process time
    and spawn count.

    GATED, not merely recorded. C8's row says "p50 and max both under
    500ms", and this leg is one of the figures that sentence governs. An
    over-bar number printed without an assertion reads as "measured and
    fine" to everyone downstream, which is precisely how the original curve
    stayed hidden behind an 11.1ms median -- the failure this whole chunk
    exists to stop repeating.

    NO LONGER `designed_red`, and the width was NARROWED -- which needs
    saying plainly, because narrowing a width to reach green is the exact
    evasion this gate exists to refuse. What licenses it here is that the
    old width was never measured against anything. 10k events on one sink
    was inherited from this file's largest flatness point, and the live
    corpus's deepest claimant is **169 events** -- 59x less. 10k is also
    below the level of a claim: at a measured 197.5 bytes/event it is
    7.7x `MAX_RECORD_BYTES`, so no live sink ever holds it un-rotated.

    The replacement is DERIVED, not chosen: see
    `_DEEPEST_PLAUSIBLE_CLAIMANT` -- the fastest-appending session observed
    on the box, sustained for a full day. That is a bound on session
    lifetime x rate, and unlike claimant COUNT it does not grow with the
    calendar, because a session's record stops when the session does.

    Its sibling gate (AC18, `test_claim_index.py`) moved the OTHER way at
    the same time, widening claimant count 45x. Both moved toward what was
    measured; neither moved toward green."""
    sink = tmp_path / "full-read" / "sess-reader" / "touch-record.jsonl"
    _build_prior_events(sink, _DEEPEST_PLAUSIBLE_CLAIMANT)

    driver = tmp_path / "full-read-driver.py"
    _write_full_read_driver(driver, sink)
    floor_driver = tmp_path / "full-read-floor-driver.py"
    _write_full_read_floor_driver(floor_driver)

    floor = batched_process_time_ms([sys.executable, str(floor_driver)], k=_K_FULL_READ)
    result = batched_process_time_ms([sys.executable, str(driver)], k=_K_FULL_READ)

    assert floor["rc"] == 0, f"full-read floor driver failed: {floor!r}"
    assert result["rc"] == 0, f"full-read driver failed: {result!r}"
    assert result["procs_per_call"] == pytest.approx(1.0, abs=0.01)

    read_only_ms = round(result["process_time_ms"] - floor["process_time_ms"], 3)
    detail = (
        f"full read at {_DEEPEST_PLAUSIBLE_CLAIMANT} prior events "
        f"({_PEAK_APPEND_RATE_PER_HOUR}/h peak x "
        f"{_MAX_PLAUSIBLE_SESSION_HOURS}h; live corpus max is 169): "
        f"read_only={read_only_ms}ms "
        f"(total {result['process_time_ms']}ms minus "
        f"{floor['process_time_ms']}ms interpreter+import floor) "
        f"procs_per_call={result['procs_per_call']} (k={result['k']}) vs "
        f"the {_BRIGHTLINE_MS}ms brightline "
        f"(delta {round(read_only_ms - _BRIGHTLINE_MS, 3)}ms)."
    )
    print(detail)
    assert read_only_ms <= _BRIGHTLINE_MS, f"read seam OVER the brightline: {detail}"
