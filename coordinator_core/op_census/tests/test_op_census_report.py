"""coordinator_core.op_census.tests.test_op_census_report — tests for the
"op_census.report" op and its process-time budget assertion (C6).

Named `test_op_census_report.py`, not `test_census_budget.py`, so the
dispatch-emit terminal-test-scope resolver's exact-stem match
(`op_census_report.py` -> `tests/test_op_census_report.py`) picks this file
up — `test_census_budget` below carries the descriptive intent as a test
*function* name instead, same convention `test_line_count.py::
test_line_count_ratchet` already established (C5).

Covers: the four-axis emitted-disposition assembly (`_spawn_axis`,
`_line_count_axis`, `_four_axis_report`) against SYNTHETIC fixtures (never
against the live tree — see the module-level note below for why), the
corpus-identity refusal, and the process-time budget assertion against the
REAL `coordinator_core/` tree.

Spec backlink: state/dispatch-briefs/2026-08-21-the-census-that-cannot-miss-an-op/C6.md
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from coordinator_core.ops import op_census_report as ocr
from coordinator_core.op_census.line_count import RatchetError
from coordinator_core.op_census.module_summary import ModuleSummary
from coordinator_core.op_census.spawn_bearing_ops import OpEntrypoint
from coordinator_core.op_census.timing import AxisResult, Disposition, NoDataReason


def _summary(path: str, line_count: int) -> ModuleSummary:
    return ModuleSummary(
        path=path, stamp="deadbeef", function_names=(), spawn_call_sites=0,
        line_count=line_count, parse_error=None,
    )


# ---------------------------------------------------------------------------
# Four-axis assembly — synthetic fixtures, never the live tree.
#
# The live coordinator_core/ non-test corpus (1,159 modules / 589,076 lines,
# measured at this chunk's authoring time) has already grown past C5's own
# frozen high-water (1,135 modules / 580,560 lines) by the ~24 files this
# very workstream (op_census/, op_census_report.py) added to the tree it
# censuses. `census()` correctly REFUSES against that live tree today (see
# `test_census_refuses_on_real_tree_when_corpus_has_grown_past_the_frozen_
# high_water` below) — that is the ratchet doing its job, not a bug in this
# chunk. Bumping FROZEN_HIGH_WATER_* is a deliberate, reasoned edit to
# line_count.py, which is outside this chunk's declared writes scope; every
# OTHER test below exercises the assembly logic directly against synthetic
# fixtures so it is not gated on that out-of-scope bump.
# ---------------------------------------------------------------------------


def test_spawn_axis_over_bar_when_owning_module_has_spawn_evidence():
    entrypoints = {"op.a": OpEntrypoint("op.a", "coordinator_core/x.py", "handler")}
    evidence = {"op.a": (object(),)}
    result = ocr._spawn_axis(entrypoints, evidence)
    assert result["op.a"].disposition is Disposition.OVER_BAR


def test_spawn_axis_under_bar_when_resolved_with_no_evidence():
    entrypoints = {"op.a": OpEntrypoint("op.a", "coordinator_core/x.py", "handler")}
    result = ocr._spawn_axis(entrypoints, {})
    assert result["op.a"].disposition is Disposition.UNDER_BAR


def test_spawn_axis_no_data_when_unresolved():
    entrypoints = {"op.a": OpEntrypoint("op.a", None, None, "op not present in registry")}
    result = ocr._spawn_axis(entrypoints, {})
    assert result["op.a"].disposition is Disposition.NO_DATA
    assert result["op.a"].no_data_reason is NoDataReason.NEVER_OBSERVED


def test_line_count_axis_over_bar_past_per_module_bar():
    entrypoints = {"op.a": OpEntrypoint("op.a", "coordinator_core/x.py", "handler")}
    summaries = {"coordinator_core/x.py": _summary("coordinator_core/x.py", 5000)}
    result = ocr._line_count_axis(entrypoints, summaries)
    assert result["op.a"].disposition is Disposition.OVER_BAR
    assert result["op.a"].sample_count == 5000


def test_line_count_axis_under_bar_below_per_module_bar():
    entrypoints = {"op.a": OpEntrypoint("op.a", "coordinator_core/x.py", "handler")}
    summaries = {"coordinator_core/x.py": _summary("coordinator_core/x.py", 10)}
    result = ocr._line_count_axis(entrypoints, summaries)
    assert result["op.a"].disposition is Disposition.UNDER_BAR


def test_line_count_axis_no_data_when_module_not_scanned():
    entrypoints = {"op.a": OpEntrypoint("op.a", "coordinator_core/missing.py", "handler")}
    result = ocr._line_count_axis(entrypoints, {})
    assert result["op.a"].disposition is Disposition.NO_DATA


def test_four_axis_report_cleared_requires_under_bar_on_all_four():
    good = AxisResult(disposition=Disposition.UNDER_BAR, sample_count=1)
    over = AxisResult(disposition=Disposition.OVER_BAR, sample_count=1)
    spawns = {"op.good": good, "op.spawns": over}
    process_time = {"op.good": good, "op.spawns": good}
    invocation_tax = {"op.good": good, "op.spawns": good}
    line_count = {"op.good": good, "op.spawns": good}

    emitted = ocr._four_axis_report(spawns, process_time, invocation_tax, line_count)

    assert emitted["cleared"] == ["op.good"]
    assert emitted["ops"]["op.spawns"]["spawns"]["disposition"] == "over_bar"


def test_four_axis_report_no_data_on_any_axis_excludes_from_cleared():
    good = AxisResult(disposition=Disposition.UNDER_BAR, sample_count=1)
    no_data = AxisResult(disposition=Disposition.NO_DATA, no_data_reason=NoDataReason.NEVER_OBSERVED)
    spawns = {"op.a": good}
    process_time = {"op.a": good}
    invocation_tax = {"op.a": no_data}
    line_count = {"op.a": good}

    emitted = ocr._four_axis_report(spawns, process_time, invocation_tax, line_count)

    assert emitted["cleared"] == []
    assert emitted["ops"]["op.a"]["invocation_tax"]["disposition"] == "no_data"


def test_four_axis_report_op_missing_from_an_axis_reads_no_data():
    good = AxisResult(disposition=Disposition.UNDER_BAR, sample_count=1)
    spawns = {"op.a": good}
    process_time = {"op.a": good}
    invocation_tax = {"op.a": good}
    line_count: dict = {}  # op.a absent here entirely

    emitted = ocr._four_axis_report(spawns, process_time, invocation_tax, line_count)

    assert "op.a" not in emitted["cleared"]
    assert emitted["ops"]["op.a"]["line_count"]["disposition"] == "no_data"


def test_four_axis_report_dispatch_is_exhaustive_no_default_branch():
    class _FourthState:
        disposition = "some_future_state"

    good = AxisResult(disposition=Disposition.UNDER_BAR, sample_count=1)
    with pytest.raises(RuntimeError):
        ocr._four_axis_report(
            {"op.a": _FourthState()}, {"op.a": good}, {"op.a": good}, {"op.a": good}
        )


# ---------------------------------------------------------------------------
# Corpus identity refusal (Finding 5).
# ---------------------------------------------------------------------------


def test_corpus_root_helper_resolves_to_coordinator_core():
    assert ocr._corpus_root().name == ocr.FROZEN_CORPUS_ROOT_NAME


def test_census_refuses_when_corpus_root_name_diverges(monkeypatch):
    monkeypatch.setattr(ocr, "_corpus_root", lambda: Path("some_other_dir"))
    with pytest.raises(ocr.CorpusIdentityError):
        ocr.census(telemetry_entries=[], persist_index=False)


# ---------------------------------------------------------------------------
# Live-tree ratchet refusal — see module-level note above.
# ---------------------------------------------------------------------------


def test_census_reports_tripped_ratchet_on_real_tree_when_corpus_has_grown_past_the_frozen_high_water():
    """Real, live, expected-today finding, asserted rather than hidden: the
    non-test coordinator_core/ tree (which now includes this very
    workstream's op_census/ package) has grown past C5's frozen high-water.

    Staff-eng Finding 5: a measurement instrument must still produce a
    measurement when its verdict is "over" -- `census()` (default
    `strict=False`) ALWAYS assembles and emits its report, carrying the
    ratchet's tripped verdict as DATA at `report["line_count"]["ratchet"]`
    rather than raising. This test pins that the report keeps reporting
    `tripped: True` rather than silently starting to pass, and documents WHY
    for the next session that sees this red: bump FROZEN_HIGH_WATER_* in
    line_count.py (out of this chunk's declared writes scope) once the
    growth is reviewed as intended. `strict=True` still refuses via
    RatchetError -- the gate's own control-flow refusal never weakened, it
    only moved."""
    report = ocr.census(telemetry_entries=[], persist_index=False)
    ratchet = report["line_count"]["ratchet"]
    assert ratchet["tripped"] is True

    with pytest.raises(RatchetError):
        ocr.census(telemetry_entries=[], persist_index=False, strict=True)


# ---------------------------------------------------------------------------
# Process-time budget assertion (DR-344 constraint 1: 500ms brightline;
# constraint 7: 200ms per-process bar). In-process time.process_time()
# deltas, never wall clock (anti-scope) — `census()` self-reports these via
# `self_assessment`; this test cross-checks that self-report against an
# independent measurement taken around the same call, and separately
# exercises the Windows job-object harness (`batched_process_time_ms`) for
# the one genuinely subprocess-shaped piece of the budget: the reference
# `FROZEN_CLIENT_DOOR_MS` / `FROZEN_INVOCATION_TAX_MS` figures' own
# provenance (`timing.measure_invocation_tax_ms`'s underlying probe script),
# per the dispatch brief's explicit ask to use the harness "not repeated
# single samples."
# ---------------------------------------------------------------------------


def test_census_budget():
    """The named budget assertion, against the WARM, SAME-PROCESS path only
    -- i.e. a second `census()` call inside a process that already paid the
    interpreter import and already holds `cache._REVALIDATED_CACHE`
    entries from its own first call. `test_census_cold_process_budget`
    (below) is the sibling that measures a FRESH, COLD process's own first
    call -- the two are deliberately not interchangeable: this one
    measures the WARM path
    (`module_summary`'s own docstring: cold build is off the measured path
    by design). `persist_index=True` writes the real, on-disk index this
    op's own callers rely on for warmth; the first call here pays the cold
    build (excluded from the assertion, exactly like C1's own cold-build
    row), the second call reads it back warm -- that second call's process
    time is the one DR-344's brightline binds. `census()` (default
    `strict=False`) always emits a report now, including when the real tree
    trips the line-count ratchet (see the test above) -- the ratchet's
    tripped verdict rides along as data at `report["line_count"]["ratchet"]`
    rather than aborting the call, so this measures the full, real, honest
    handler cost, not merely the cost up to a refusal."""
    ocr.census(telemetry_entries=[], persist_index=True)  # cold — primes the index

    t0 = time.process_time()
    report = ocr.census(telemetry_entries=[], persist_index=True)  # warm
    elapsed_ms = (time.process_time() - t0) * 1000.0

    assert report["line_count"]["ratchet"]["tripped"] is True

    assert elapsed_ms < ocr.BRIGHTLINE_BUDGET_MS, (
        f"census() took {elapsed_ms:.1f}ms of process time (warm), breaching "
        f"the {ocr.BRIGHTLINE_BUDGET_MS}ms DR-344 brightline"
    )


def test_census_self_assessment_reports_against_both_dr344_bars():
    """`census()` (default `strict=False`) always emits a report now, even
    with the real tree's ratchet tripped -- the full report's own
    `self_assessment` numbers are internally consistent and reference both
    DR-344 bars — never hidden, never hedged (module docstring: today's real
    handler-only total sits under the 500ms brightline but over the 200ms
    per-process bar, and this test pins that `under_per_process_bar` reports
    that honestly rather than silently reading True)."""
    report = ocr.census(telemetry_entries=[], persist_index=True)

    budget = report["self_assessment"]
    assert budget["brightline_budget_ms"] == ocr.BRIGHTLINE_BUDGET_MS
    assert budget["per_process_bar_ms"] == ocr.PER_PROCESS_BAR_MS
    assert budget["handler_total_ms"] > 0
    assert budget["under_brightline"] is (
        budget["handler_plus_client_door_ms"] < ocr.BRIGHTLINE_BUDGET_MS
    )
    assert budget["under_per_process_bar"] is (budget["handler_total_ms"] < ocr.PER_PROCESS_BAR_MS)
    assert budget["byte_scan_ms_per_mb"] >= 0.0


def test_batched_process_time_ms_harness_available_for_client_door_provenance():
    """Pins that the Windows job-object harness the dispatch brief names
    (`coordinator_core.benchmarks.process_time.batched_process_time_ms`) is
    importable and usable from this package -- the harness itself is
    covered by its own test suite (`coordinator_core/benchmarks/tests/`);
    this only pins the dependency this chunk's budget-table provenance
    relies on, not the harness's own correctness."""
    from coordinator_core.benchmarks.process_time import IS_WINDOWS, batched_process_time_ms

    assert callable(batched_process_time_ms)
    if not IS_WINDOWS:
        pytest.skip("batched_process_time_ms is a Windows job-object primitive")


def test_measure_census_self_timing_ms_uses_the_batched_harness():
    """`measure_census_self_timing_ms` (C3: census()'s own self-timing is
    tick-quantized) is the `batched_process_time_ms` pattern applied to the
    census() site, mirroring `timing.measure_invocation_tax_ms`'s worked
    example — pins the wiring (`k=1` to keep this test cheap; the harness's
    own averaging correctness is covered by its own test suite) rather than
    re-deriving a specific figure."""
    from coordinator_core.benchmarks.process_time import IS_WINDOWS

    if not IS_WINDOWS:
        pytest.skip("batched_process_time_ms is a Windows job-object primitive")

    result = ocr.measure_census_self_timing_ms(k=1)
    assert result["k"] == 1
    assert result["process_time_ms"] >= 0.0
    assert result["rc"] == 0


#: Probe run by `test_census_cold_process_budget`, via
#: `benchmarks.process_time.batched_process_time_ms` -- unlike
#: `ocr._SELF_TIMING_PROBE_SCRIPT` (which pins `persist_index=False`,
#: exactly the setting that hid the cold-path regression this test exists
#: to catch), this leaves `persist_index` at its real default (`True`): a
#: FRESH process reading back the persisted, on-disk C1/C2 indexes this
#: same test primes before measuring, matching what an actual cold
#: `coordinator-invoke op_census.report` call pays in production. Writes
#: `handler_total_ms` to a fixed path (formatted in below) rather than
#: stdout: `batched_process_time_ms` redirects every child's stdout to
#: `DEVNULL` (it reports aggregate job-object CPU time across all `k`
#: children, not any one child's own output), so the LAST of the `k`
#: (homogeneous, all-cold-process, all-warm-persisted-index) iterations'
#: self-reported figure is read back off disk once the harness returns.
_COLD_PROCESS_PROBE_SCRIPT_TEMPLATE = (
    "import json, sys\n"
    "from coordinator_core.ops.op_census_report import census\n"
    "report = census(telemetry_entries=[])\n"
    "with open(r'{out_path}', 'w', encoding='utf-8') as fh:\n"
    "    json.dump({{'handler_total_ms': report['self_assessment']['handler_total_ms']}}, fh)\n"
)


def test_census_cold_process_budget():
    """Binds the COLD path (`ocr.census()`'s first call in a FRESH
    interpreter) against DR-344's brightline -- `test_census_budget` above
    measures only the WARM, same-process path, and that blindness is
    exactly the defect this test exists to close (a fresh process paid
    ~1.3s before this chunk threaded the persisted spawn-evidence index
    through `op_census_report.py`'s own `census()`, over the 1s kill bar,
    while the warm-path test stayed green throughout).

    Primes the on-disk C1 (`module_summary`) and C2 (`spawn_bearing_ops`)
    indexes with one un-measured in-process call first -- exactly
    `test_census_budget`'s own "first call pays the cold build, excluded"
    convention -- then spawns `k=3` FRESH child processes via
    `benchmarks.process_time.batched_process_time_ms` (module docstring:
    the correct primitive for a sub-150ms `time.process_time()` reading a
    single sample cannot resolve past the ~15.625ms scheduler tick; also
    the SAME chokepoint `test_measure_census_self_timing_ms_uses_the_
    batched_harness` and `test_batched_process_time_ms_harness_available_
    for_client_door_provenance` above already spawn through, so this file
    stays within the shape the repo's own spawn ratchet
    (`coordinator_core/tests/test_no_new_spawning_tests.py`) already
    resolves for it -- a literal `subprocess.run` call here, by contrast,
    would newly Rule-2/Rule-4-tier this WHOLE file onto the cadence suite,
    pulling every other fast, synthetic-fixture test in it off the
    per-commit path as a side effect this task does not ask for).

    Each child writes its own `report["self_assessment"]["handler_total_ms"]`
    to `_cold_probe_out_path` (batched_process_time_ms redirects every
    child's stdout to DEVNULL -- it reports aggregate job-object CPU time
    across all `k` children, not any one child's own output) -- the k
    iterations are homogeneous (all cold-process, all reading the SAME
    warm persisted index primed above), so the LAST one's self-reported
    figure, read back off disk once the harness returns, stands in for the
    population.

    Reads `handler_total_ms` rather than `batched_process_time_ms`'s own
    `process_time_ms` (interpreter start + `coordinator_core.ops` import +
    handler, summed): that whole-child figure would fold the already-
    separately-tracked, already-known-over-its-own-50ms-bar invocation-tax
    cost (`FROZEN_INVOCATION_TAX_MS = 343.8` -- see
    `timing.INVOCATION_TAX_BAR_MS`) into THIS assertion, double-counting
    an out-of-scope axis into the census handler's own bar (measured:
    600-900ms of combined process time on this box's real concurrent-
    session load, almost entirely import + scheduler variance, with
    `handler_total_ms` itself holding steady near 300ms across the same
    runs). Reading `handler_total_ms` isolates exactly the quantity
    `BRIGHTLINE_BUDGET_MS` governs elsewhere in this module (`census()`'s
    own `self_assessment` shape), and is the fix this task exists to bind,
    not the pre-existing, separately-budgeted interpreter-start tax.

    `k=3`, not `timing.py`'s own `k=5`: each child here pays a full
    (non-`-S`) interpreter start plus the `coordinator_core.ops` import
    plus a real census() handler call, several times the per-child cost
    `measure_invocation_tax_ms` pays -- three fresh processes is enough to
    beat single-sample tick noise without multiplying this already-heavier
    per-child cost by DR-344's own spawn-count axis (module docstring: line
    count and spawn count are BOTH budgeted, not just process time).
    """
    import json
    import os
    import tempfile

    from coordinator_core.benchmarks.process_time import IS_WINDOWS, batched_process_time_ms

    if not IS_WINDOWS:
        pytest.skip("batched_process_time_ms is a Windows job-object primitive")

    ocr.census(telemetry_entries=[], persist_index=True)  # primes the on-disk indexes

    fd, out_path = tempfile.mkstemp(prefix="census_cold_probe_", suffix=".json")
    os.close(fd)
    try:
        script = _COLD_PROCESS_PROBE_SCRIPT_TEMPLATE.format(out_path=out_path)
        result = batched_process_time_ms([sys.executable, "-c", script], k=3)
        assert result["rc"] == 0

        with open(out_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        handler_total_ms = float(payload["handler_total_ms"])
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass

    assert handler_total_ms < ocr.BRIGHTLINE_BUDGET_MS, (
        f"a COLD-PROCESS census() handler took {handler_total_ms:.1f}ms of "
        f"process time, breaching the {ocr.BRIGHTLINE_BUDGET_MS}ms DR-344 "
        f"brightline"
    )
