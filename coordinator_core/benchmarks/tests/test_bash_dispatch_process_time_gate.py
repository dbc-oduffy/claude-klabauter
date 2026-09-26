"""Standing gate + baseline capture for pln-guards-under-the-brightline's
C1: the derived process-time floor (AC2), the full spawn enumeration (AC6),
and the three pre-change baselines (AC9/AC10/AC11) C2/C3 exit-gate against.

THIS FILE CHANGES NO GUARD BEHAVIOUR. It exists so "before" is obtainable
for every downstream chunk's pre/post comparison -- see this chunk's own
dispatch brief.

UNIT: process time (job-object `TotalUserTime + TotalKernelTime`), never
wall clock -- `coordinator_core.benchmarks.process_time` module docstring
names the three measurement traps this avoids; `bash_dispatch_probe.py`'s
own module docstring names why this is the right instrument for this
entrypoint and not `gate.py`/`budget.py`/`harness.py` (a different,
wall-clock-gated contract this plan's Anti-scope forbids reusing here).

THE AC1 CEILING, AND WHY IT IS THE BRIGHTLINE ITSELF, NOT A TIGHTER NUMBER.
`DISPATCH_PROCESS_TIME_CEILING_MS` is set at DR-344's own ratified 500ms
brightline (`docs/decisions/DR-344-the-brightline-process-budget-for-
Claude-klabauter.md`), not at a margin over the derived floor the way
`test_warm_door_process_time_gate.py`'s `PROCESS_TIME_CEILING_MS` is. AC1's
own text is "a process-time gate ... exists and fails on regression" --
this chunk changes no guard behaviour, so its gate must PASS today, before
C2/C3 land, against the measured pre-fix cost the research doc records
(469.5ms across 6.8 processes for the plumbing-shaped call) -- and DR-344
forbids moving the bar to accommodate a miss, so the only number this gate
can honestly assert YET is the bar itself. AC3/AC4/AC5's TIGHTER
floor-plus-margin thresholds are a different, later assertion: they belong
to C2/C3, the chunks that actually reduce the cost, and each is written at
its own point of use in THOSE chunks' own test files once the fix it
verifies has landed -- writing a floor-derived ceiling here, before either
fix exists, would either fail immediately (if set near the floor) or be a
number nobody has verified is reachable (if set anywhere else), which is
exactly the "round number picked for comfort" this plan's AC table warns
against.

DERIVED FLOOR NAMED CONSTANTS (AC2). `_FLOOR_MARGIN_MS_*` below are NOT
consumed as gates in this file -- they are recorded here, next to the
floor measurement that grounds them, so a later reader (C2/C3, or a human
re-deriving a threshold) can see which number is MEASURED
(`FloorMeasurement`'s three legs) and which is JUDGEMENT (the margin), per
this chunk's own dispatch brief: "Write the floor and the margins into the
test as named constants with the reasoning beside them."

WINDOWS AND DARWIN, STATED EXPLICITLY (C9, docs/plans/2026-08-22-the-
brightlines-instrument-exists-on-the-fleet-floor.md, AC20/AC24). Every
process-time assertion in this file skips off both: `batched_process_time_
ms` implements a Windows job-object primitive and a Darwin kqueue+`wait4`
primitive, nothing else (`process_time.py` module docstring); DR-344's
brightline itself carries no platform scoping (checked against the ruling
text directly), so `DISPATCH_PROCESS_TIME_CEILING_MS` below is the SAME
500ms constant on both platforms -- the Windows figure is UNTOUCHED by
this addition. What is per-platform is the MEASURED evidence that the
dispatch entrypoint clears the bar, not the bar itself: measured this
session (2026-08-22, this box, Darwin, `batched_process_time_quantiles
(k=20, n=15)` per corpus row, AC20's "measured pin cites n, p50, p90, and
the date"): `bash_echo_hello` p50=60.1ms/p90=62.1ms, `bash_cat_pyproject_
head` p50=64.5ms/p90=71.2ms, `powershell_get_childitem` p50=61.9ms/
p90=65.9ms -- every row comfortably under the 500ms brightline, with wide
headroom (>6x at p90).

THE TWO HARNESS TRAPS THIS FILE AVOIDS (already burned in this repo,
`process_time.py` module docstring):
  1. K=20 per job object, never a single sample -- the ~15.6ms scheduler-
     tick quantisation `batched_process_time_ms` exists to amortise past.
  2. `batched_process_time_ms` reports only the LAST invocation's `rc` --
     `bash_dispatch_probe._verify_single_invocation_succeeds` runs one
     untimed, unbatched, `check=True` invocation first, so a script that
     cannot even complete once fails loudly rather than hiding inside a
     batched average that only ever looked at the final sample.

BASELINE CAPTURES ARE SELF-CONSISTENCY CHECKS, NOT YET PRE/POST
COMPARISONS. C2/C3 own the actual "does this match C1's baseline"
assertion (AC9/AC10/AC11 are THEIR exit gates, per the plan's own AC
table) -- this file's own baseline tests only prove each capture function
is deterministic and importable, and record the values for a later chunk
to import via `bash_dispatch_probe.capture_message_baseline` /
`capture_roster_baseline` / `capture_executed_set_baseline`, not by
duplicating a second copy of the data as a module-level literal here (the
capture FUNCTIONS themselves are the committed golden artifact's
production seam; a chunk that wants a frozen snapshot to diff against
calls them, exactly as this file does).

Spec backlink: docs/plans/2026-08-21-guards-under-the-brightline.md, chunk
C1.
"""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks.bash_dispatch_probe import (
    CORPUS_PAYLOADS,
    _dispatch_cmd,
    _verify_single_invocation_succeeds,
    capture_executed_set_baseline,
    capture_message_baseline,
    capture_roster_baseline,
    enumerate_spawn_set_for_corpus,
    measure_derived_floor,
)
from coordinator_core.benchmarks.process_time import (
    IS_DARWIN,
    IS_WINDOWS,
    batched_process_time_ms,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_INVOCATIONS = 20
"""Amortisation factor recovering sub-tick resolution -- see
`coordinator_core.benchmarks.process_time` module docstring, trap 2."""

DISPATCH_PROCESS_TIME_CEILING_MS = 500.0
"""DR-344's own ratified brightline -- see this module's docstring, "THE
AC1 CEILING, AND WHY IT IS THE BRIGHTLINE ITSELF" for why this file asserts
the bar itself rather than a tighter, unverified number."""

_FLOOR_MARGIN_MS_TIGHT = 20.0
"""A margin appropriate for a threshold with NO guard-side spawn between
the floor and the observed cost (e.g. a corpus command no advisory/hard-
deny guard's own detection touches at all) -- covers scheduler-tick noise
and ordinary box-load jitter only. Not consumed as a gate in this file;
recorded for C2/C3's own point-of-use derivation (AC3-AC5)."""

_FLOOR_MARGIN_MS_ONE_GIT_SPAWN = 120.0
"""A margin appropriate for a threshold whose remaining cost is exactly ONE
memoized `git rev-parse --show-toplevel` (Site 2's per-process memo, still
paid once per hook process even after C3's short-circuit fix) -- Finding 2
of the research doc measures a single such git spawn's own process-time
contribution in this class; not consumed as a gate in this file, recorded
for C3's own point-of-use derivation (AC8)."""


def _require_supported_platform() -> None:
    if not (IS_WINDOWS or IS_DARWIN):
        pytest.skip(
            "process-time accounting has no primitive for this platform "
            "-- see coordinator_core.benchmarks.process_time module docstring"
        )


_KNOWN_OVER_BUDGET_PRE_FIX: set = set()


@pytest.mark.parametrize(
    "label",
    [
        pytest.param(label, marks=pytest.mark.pending_fix)
        if label in _KNOWN_OVER_BUDGET_PRE_FIX
        else label
        for label in sorted(CORPUS_PAYLOADS)
    ],
)
def test_dispatch_entrypoint_process_time_is_under_the_brightline(label):
    _require_supported_platform()
    payload = CORPUS_PAYLOADS[label]
    argv, env = _dispatch_cmd(payload)
    _verify_single_invocation_succeeds(argv, env)
    result = batched_process_time_ms(argv, k=K_INVOCATIONS, env=env)
    assert result["process_time_ms"] <= DISPATCH_PROCESS_TIME_CEILING_MS, (
        f"{label}: dispatch entrypoint process time "
        f"{result['process_time_ms']}ms exceeds the "
        f"{DISPATCH_PROCESS_TIME_CEILING_MS}ms DR-344 brightline "
        f"(k={result['k']}, procs_per_call={result['procs_per_call']}, "
        f"wall_ms={result['wall_ms']} for context only)."
    )


def test_derived_floor_components_are_monotonically_increasing():
    _require_supported_platform()
    floor = measure_derived_floor(k=K_INVOCATIONS)
    bare_ms = floor.bare_interpreter["process_time_ms"]
    closure_ms = floor.import_closure["process_time_ms"]
    chain_ms = floor.chain_spawns_nothing["process_time_ms"]
    assert floor.bare_interpreter["rc"] == 0
    assert floor.import_closure["rc"] == 0
    assert floor.chain_spawns_nothing["rc"] == 0
    tolerance_ms = 15.6
    assert bare_ms <= closure_ms + tolerance_ms, (
        f"bare_interpreter ({bare_ms}ms) unexpectedly costlier than "
        f"import_closure ({closure_ms}ms) by more than one scheduler tick"
    )
    assert closure_ms <= chain_ms + tolerance_ms, (
        f"import_closure ({closure_ms}ms) unexpectedly costlier than "
        f"chain_spawns_nothing ({chain_ms}ms) by more than one scheduler tick"
    )
    assert chain_ms <= DISPATCH_PROCESS_TIME_CEILING_MS, (
        f"chain_spawns_nothing ({chain_ms}ms) -- the floor no guard-side "
        f"fix can ever reduce -- already exceeds the "
        f"{DISPATCH_PROCESS_TIME_CEILING_MS}ms brightline; if this ever "
        "fires, that is a genuine PM-altitude finding (DR-344 forbids "
        "moving the bar to accommodate it), not a test to loosen."
    )


def test_spawn_set_enumeration_completes_and_every_record_has_a_stack():
    spawns = enumerate_spawn_set_for_corpus()
    assert set(spawns) == set(CORPUS_PAYLOADS)
    for label, records in spawns.items():
        for record in records:
            assert record.argv, f"{label}: a spawn record with an empty argv"
            assert record.stack, f"{label}: a spawn record with an empty call stack"


def test_powershell_get_childitem_spawns_nothing_registered_guard_can_see():
    spawns = enumerate_spawn_set_for_corpus()
    assert spawns["powershell_get_childitem"] == []


def test_message_baseline_capture_is_non_empty_and_every_row_is_a_non_negative_int():
    first = capture_message_baseline()
    assert first, "message baseline capture produced no rows at all"
    for key, value in first.items():
        assert isinstance(value, int) and value >= 0, (
            f"{key}: message byte count {value!r} is not a non-negative int"
        )


def test_message_baseline_substitutes_out_the_resolved_interpreter_prefix():
    from coordinator_core.bash_guards import dispatch_checks
    from coordinator_core.benchmarks import bash_dispatch_probe as probe

    original = dispatch_checks._bt_python3_invocation
    try:
        dispatch_checks._bt_python3_invocation = (
            lambda: r"C:\some\very\long\resolved\path\python.exe"
        )
        prefix = probe._resolved_python3_invocation_prefix()
        text_with_prefix = "run: %s -c 'do_thing()'" % prefix
        text_with_placeholder = "run: <PYTHON3_INVOCATION> -c 'do_thing()'"
        substituted = text_with_prefix.replace(prefix, "<PYTHON3_INVOCATION>")
        assert len(substituted.encode("utf-8")) == len(
            text_with_placeholder.encode("utf-8")
        )
        assert prefix not in substituted
    finally:
        dispatch_checks._bt_python3_invocation = original


def test_roster_baseline_capture_is_deterministic_and_carries_every_field():
    first = capture_roster_baseline()
    second = capture_roster_baseline()
    assert first, "roster baseline capture produced no entries at all"
    assert first == second, (
        "capture_roster_baseline() is not deterministic across two calls "
        "in the same process -- C2/C3's exit gate (AC10) depends on this."
    )
    for entry in first:
        assert set(entry) == {"id", "matchers", "band", "fail_closed", "script"}, (
            f"roster entry {entry.get('id')!r} is missing a field AC10 "
            "requires (id, matchers, band, fail_closed, script)"
        )


def test_executed_set_baseline_capture_is_deterministic():
    first = capture_executed_set_baseline()
    second = capture_executed_set_baseline()
    assert set(first) == set(CORPUS_PAYLOADS)
    assert first == second, (
        "capture_executed_set_baseline() is not deterministic across two "
        "calls in the same process -- C2/C3's exit gate (AC11) depends on "
        "this function returning the same executed set and verdicts for "
        "the same tree state."
    )
    for label, result in first.items():
        assert set(result) == {"executed", "verdicts"}
        assert set(result["verdicts"]) <= set(result["executed"]), (
            f"{label}: a verdict recorded for a guard not in the executed "
            "set -- the tracer wrapped a guard that never ran"
        )
