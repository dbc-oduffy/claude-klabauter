"""
coordinator_core.ops.tests.test_gate_dimension_latency

Op-level tests for the "latency" dimension check (C7,
docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C7), registered
via `gate_dimension_latency.register_dimension("latency", ...)`.

Coverage:
  - self-registration: importing the module plugs a real check into
    `gate_validate_invocable`'s "latency" slot (no longer the stub).
  - self-exclusion: a changed-file set containing only the validator's own
    module/test files (or this dimension's own files) never reaches a
    gating verdict for them.
  - re-entrancy sentinel: a nested call raises loudly, and the outer
    `_run_dimension` wrapper turns that into a visible ERROR, never a
    silent pass.
  - op mapping + verdict aggregation: COMPUTE_ONLY pass/fail from a
    persisted ConformanceRecord, MUTATING is always advisory-only (never
    flips the dimension), an unmapped changed file yields UNAVAILABLE, and
    a never-benchmarked mapped op is unavailable-for-that-op without
    flipping the whole dimension red.

Spec backlink: docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C7
"""

from __future__ import annotations

import hashlib
import json

import pytest

# ---------------------------------------------------------------------------
# Import guards — MUST precede any test so both modules' registration side
# effects fire (gate_validate_invocable's @register_op, then this module's
# register_dimension("latency", ...) call).
# ---------------------------------------------------------------------------
import coordinator_core.ops.gate_validate_invocable  # noqa: F401
import coordinator_core.ops.gate_dimension_latency as latency_dim  # noqa: E402

from coordinator_core.authz.classification import OpClass
from coordinator_core.benchmarks import baseline_store
from coordinator_core.benchmarks.record import ConformanceRecord, Tolerance, compose_machine_id
from coordinator_core.ops.gate_validate_invocable import (
    Verdict,
    _DIMENSION_REGISTRY,
    _gate_validate_invocable,
    _run_dimension,
)


@pytest.fixture(autouse=True)
def _restore_dimension_registry():
    """Isolate registry mutations across tests, mirroring
    test_gate_validate_invocable.py's own fixture."""
    original = dict(_DIMENSION_REGISTRY)
    yield
    _DIMENSION_REGISTRY.clear()
    _DIMENSION_REGISTRY.update(original)
    latency_dim._REENTRANCY_GUARD.set(False)


def _record(
    op="fake.op",
    verdict_source="pass",
    sample_count=10,
    code_sha="a" * 40,
    live_sessions=5,
    machine=None,
):
    """Build a minimal ConformanceRecord whose gate.evaluate() outcome is
    steered by target_ms relative to a fixed gating_statistic_value.

    `live_sessions` defaults to 5 (well inside C1's low band) so this
    fixture's records read as trusted-band by default; pass `None` or a
    value > 45 to exercise the C4 load-skew downgrade explicitly.

    `machine` defaults to this box's own compose_machine_id() (C3's machine
    partition) — a record with `machine is None` is never yielded by
    `baseline_store.query()` regardless of the caller's own `machine=`
    filter, so an end-to-end test that round-trips through the real store
    must set this or the record silently vanishes on read-back."""
    if machine is None:
        machine = compose_machine_id()
    gating_value = 50.0
    if verdict_source == "pass":
        target_ms = 100.0
    elif verdict_source == "fail":
        target_ms = 1.0
    else:
        raise ValueError(verdict_source)
    return ConformanceRecord(
        op=op,
        op_class="COMPUTE_ONLY",
        target_ms=target_ms,
        tolerance=Tolerance(kind="relative", value=0.2),
        gating_statistic="min",
        gating_statistic_value=gating_value,
        min=gating_value,
        p50=gating_value,
        p95=gating_value,
        p99=gating_value,
        sample_count=sample_count,
        cold_start_floor_ms=10.0,
        floor_delta_ms=gating_value - 10.0,
        floor_cov=0.01,
        run_id="run-1",
        verdict=verdict_source,
        baseline_id="b-1",
        code_sha=code_sha,
        timestamp="2026-08-14T00:00:00+00:00",
        ambient_before={"live_sessions": live_sessions},
        machine=machine,
    )


def _inventory(tmp_path, entries):
    path = tmp_path / "op-inventory.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------


def test_latency_slot_is_registered_not_stub():
    assert _DIMENSION_REGISTRY["latency"] is latency_dim._check_latency


# ---------------------------------------------------------------------------
# Self-exclusion
# ---------------------------------------------------------------------------


def test_self_excluded_paths_never_reach_op_mapping(monkeypatch):
    monkeypatch.setattr(
        latency_dim,
        "_load_op_inventory",
        lambda: [
            {
                "op_key": "gate.validate_invocable",
                "module_path": "coordinator_core/ops/gate_validate_invocable.py",
                "classification": "COMPUTE_ONLY",
            }
        ],
    )
    changed = [
        "coordinator_core/ops/gate_validate_invocable.py",
        "coordinator_core/ops/tests/test_gate_validate_invocable.py",
        "coordinator_core/ops/gate_dimension_latency.py",
        "coordinator_core/ops/tests/test_gate_dimension_latency.py",
    ]
    op_map = latency_dim._map_paths_to_ops(changed)
    assert op_map == {}


def test_only_validator_changed_yields_unavailable_not_gating():
    result = _gate_validate_invocable(
        {"changed_files": ["coordinator_core/ops/gate_validate_invocable.py"]}
    )
    latency = next(d for d in result["dimensions"] if d["dimension"] == "latency")
    assert latency["verdict"] == Verdict.UNAVAILABLE.value


# ---------------------------------------------------------------------------
# Re-entrancy sentinel
# ---------------------------------------------------------------------------


def test_reentrant_call_raises_loudly():
    latency_dim._REENTRANCY_GUARD.set(True)
    try:
        with pytest.raises(latency_dim.LatencyDimensionReentrancyError):
            latency_dim._check_latency(["x.py"], None, None)
    finally:
        latency_dim._REENTRANCY_GUARD.set(False)


def test_reentrant_call_via_run_dimension_reads_as_error_not_silent_pass():
    latency_dim._REENTRANCY_GUARD.set(True)
    try:
        result = _run_dimension("latency", ["x.py"], None, None)
    finally:
        latency_dim._REENTRANCY_GUARD.set(False)
    assert result.verdict is Verdict.ERROR
    assert "LatencyDimensionReentrancyError" in result.detail


def test_guard_is_reset_after_a_normal_call():
    assert latency_dim._REENTRANCY_GUARD.get() is False
    latency_dim._check_latency([], None, None)
    assert latency_dim._REENTRANCY_GUARD.get() is False


# ---------------------------------------------------------------------------
# Op mapping + verdict aggregation
# ---------------------------------------------------------------------------


def test_unmapped_changed_file_is_unavailable(monkeypatch):
    monkeypatch.setattr(latency_dim, "_load_op_inventory", lambda: [])
    result = latency_dim._check_latency(["docs/some-doc.md"], None, None)
    assert result.dimension == "latency"
    assert result.verdict is Verdict.UNAVAILABLE


def test_compute_only_op_with_passing_record_gates_pass(monkeypatch):
    monkeypatch.setattr(
        latency_dim,
        "_load_op_inventory",
        lambda: [
            {
                "op_key": "fake.op",
                "module_path": "coordinator_core/ops/fake_op.py",
                "classification": "COMPUTE_ONLY",
            }
        ],
    )
    monkeypatch.setattr(
        latency_dim, "_latest_record_for", lambda op_key: _record(op=op_key, verdict_source="pass")
    )
    result = latency_dim._check_latency(["coordinator_core/ops/fake_op.py"], None, None)
    assert result.verdict is Verdict.PASS
    assert "fake.op" in result.detail


def test_compute_only_op_with_failing_record_gates_fail(monkeypatch):
    monkeypatch.setattr(
        latency_dim,
        "_load_op_inventory",
        lambda: [
            {
                "op_key": "fake.op",
                "module_path": "coordinator_core/ops/fake_op.py",
                "classification": "COMPUTE_ONLY",
            }
        ],
    )
    monkeypatch.setattr(
        latency_dim, "_latest_record_for", lambda op_key: _record(op=op_key, verdict_source="fail")
    )
    result = latency_dim._check_latency(["coordinator_core/ops/fake_op.py"], None, None)
    assert result.verdict is Verdict.FAIL


def test_failing_record_with_null_ambient_downgrades_to_advisory(monkeypatch):
    monkeypatch.setattr(
        latency_dim,
        "_load_op_inventory",
        lambda: [
            {
                "op_key": "fake.op",
                "module_path": "coordinator_core/ops/fake_op.py",
                "classification": "COMPUTE_ONLY",
            }
        ],
    )
    monkeypatch.setattr(
        latency_dim,
        "_latest_record_for",
        lambda op_key: _record(op=op_key, verdict_source="fail", live_sessions=None),
    )
    result = latency_dim._check_latency(["coordinator_core/ops/fake_op.py"], None, None)
    assert result.verdict is not Verdict.FAIL
    assert "downgraded from fail" in result.detail


def test_failing_record_with_high_load_ambient_downgrades_to_advisory(monkeypatch):
    monkeypatch.setattr(
        latency_dim,
        "_load_op_inventory",
        lambda: [
            {
                "op_key": "fake.op",
                "module_path": "coordinator_core/ops/fake_op.py",
                "classification": "COMPUTE_ONLY",
            }
        ],
    )
    monkeypatch.setattr(
        latency_dim,
        "_latest_record_for",
        lambda op_key: _record(op=op_key, verdict_source="fail", live_sessions=46),
    )
    result = latency_dim._check_latency(["coordinator_core/ops/fake_op.py"], None, None)
    assert result.verdict is not Verdict.FAIL
    assert "downgraded from fail" in result.detail


def test_mutating_op_never_gates_even_with_bad_latency(monkeypatch):
    monkeypatch.setattr(
        latency_dim,
        "_load_op_inventory",
        lambda: [
            {
                "op_key": "fake.mutating_op",
                "module_path": "coordinator_core/ops/fake_mutating_op.py",
                "classification": "MUTATING",
            }
        ],
    )

    def _boom(op_key):
        raise AssertionError("MUTATING op must not read a benchmark record at all")

    monkeypatch.setattr(latency_dim, "_latest_record_for", _boom)
    result = latency_dim._check_latency(["coordinator_core/ops/fake_mutating_op.py"], None, None)
    assert result.verdict is Verdict.UNAVAILABLE
    assert "MUTATING" in result.detail
    assert "advisory" in result.detail


def test_never_benchmarked_compute_only_op_is_unavailable_not_fail(monkeypatch):
    monkeypatch.setattr(
        latency_dim,
        "_load_op_inventory",
        lambda: [
            {
                "op_key": "fake.never_run",
                "module_path": "coordinator_core/ops/fake_never_run.py",
                "classification": "COMPUTE_ONLY",
            }
        ],
    )
    monkeypatch.setattr(latency_dim, "_latest_record_for", lambda op_key: None)
    result = latency_dim._check_latency(["coordinator_core/ops/fake_never_run.py"], None, None)
    assert result.verdict is Verdict.UNAVAILABLE


def test_op_class_resolution_returns_none_for_unclassified_op(monkeypatch):
    monkeypatch.setattr(
        latency_dim,
        "_load_op_inventory",
        lambda: [
            {
                "op_key": "fake.unclassified",
                "module_path": "x.py",
                "classification": None,
            }
        ],
    )
    assert latency_dim._op_class_for("fake.unclassified") is None
    assert latency_dim._op_class_for("nonexistent.op") is None


def test_unclassified_op_is_unavailable_not_gated(monkeypatch):
    monkeypatch.setattr(
        latency_dim,
        "_load_op_inventory",
        lambda: [
            {
                "op_key": "percolate.run_ci_smoke_check",
                "module_path": "coordinator_core/ops/fake_unclassified_op.py",
                "classification": None,
            }
        ],
    )

    def _boom(op_key):
        raise AssertionError("an unclassified op must not read a benchmark record at all")

    monkeypatch.setattr(latency_dim, "_latest_record_for", _boom)
    result = latency_dim._check_latency(
        ["coordinator_core/ops/fake_unclassified_op.py"], None, None
    )
    assert result.verdict is Verdict.UNAVAILABLE
    assert "percolate.run_ci_smoke_check" in result.detail
    assert "classification" in result.detail


# ---------------------------------------------------------------------------
# End-to-end (C6): a record produced through the real record.py path,
# persisted via the real baseline_store.append() code path to an isolated
# per-test store partition, read back through gate_validate_invocable's
# dimension seam (real gate.evaluate + real budget-manifest min_gating_sample
# resolution) — proving the gate actually goes red, not merely that a
# monkeypatched _latest_record_for can be asserted FAIL in isolation.
#
# STORE-PATH ISOLATION (staff-eng second-pass review, R6): `_latest_record_for`
# calls `baseline_store.query(op=...)` with no explicit path, so it always
# reads `baseline_store.DEFAULT_STORE_PATH` — the real, tracked, in-repo
# partition. Every test below monkeypatches that module attribute to a
# tmp_path partition (the real *code path*, never the real *file*) and
# asserts the tracked partition's bytes are unchanged by the run.
#
# Non-vacuity: a plain over-budget record going FAIL is not itself a C4
# guard (C4 only makes FAIL harder under band skew, never easier — that
# case still fails with C4 reverted). The non-vacuous case is the
# complement: a record whose own ambient band is unknown or high-load must
# NOT go red — that fails with C4 reverted. Both are asserted below,
# end-to-end.
# ---------------------------------------------------------------------------


def _hash_file(path):
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_tracked_partition_untouched(real_store_path, before_hash):
    assert _hash_file(real_store_path) == before_hash, (
        "the real, tracked baseline.jsonl partition was mutated by a test — "
        "store-path isolation (R6) failed to redirect the write"
    )


def test_end_to_end_over_budget_record_gates_fail_through_real_store(monkeypatch, tmp_path):
    real_store_path = baseline_store.DEFAULT_STORE_PATH
    before_hash = _hash_file(real_store_path)
    monkeypatch.setattr(baseline_store, "DEFAULT_STORE_PATH", tmp_path / "baseline.jsonl")

    record = _record(
        op="e2e.over_budget.op",
        verdict_source="fail",
        sample_count=10,
        live_sessions=5,
    )
    written_path = baseline_store.append(record)
    assert written_path == tmp_path / "baseline.jsonl"
    assert written_path != real_store_path

    monkeypatch.setattr(
        latency_dim,
        "_load_op_inventory",
        lambda: [
            {
                "op_key": "e2e.over_budget.op",
                "module_path": "coordinator_core/ops/fake_e2e_op.py",
                "classification": "COMPUTE_ONLY",
            }
        ],
    )

    result = latency_dim._check_latency(["coordinator_core/ops/fake_e2e_op.py"], None, None)

    assert result.verdict is Verdict.FAIL
    assert "e2e.over_budget.op" in result.detail
    assert "floor_delta_ms" not in result.detail
    assert record.floor_delta_ms is not None  # sanity: the field is populated but unused
    _assert_tracked_partition_untouched(real_store_path, before_hash)


def test_end_to_end_below_min_gating_sample_count_is_advisory_not_fail(monkeypatch, tmp_path):
    real_store_path = baseline_store.DEFAULT_STORE_PATH
    before_hash = _hash_file(real_store_path)
    monkeypatch.setattr(baseline_store, "DEFAULT_STORE_PATH", tmp_path / "baseline.jsonl")

    # budget-manifest.json's real min_gating_sample_count is 5 (unmocked —
    # this exercises the real resolver, not a stand-in).
    manifest = latency_dim.budget_mod.load_manifest()
    assert 3 < manifest["min_gating_sample_count"]

    record = _record(
        op="e2e.underpowered.op",
        verdict_source="fail",
        sample_count=3,
        live_sessions=5,
    )
    baseline_store.append(record)

    monkeypatch.setattr(
        latency_dim,
        "_load_op_inventory",
        lambda: [
            {
                "op_key": "e2e.underpowered.op",
                "module_path": "coordinator_core/ops/fake_e2e_underpowered_op.py",
                "classification": "COMPUTE_ONLY",
            }
        ],
    )

    result = latency_dim._check_latency(
        ["coordinator_core/ops/fake_e2e_underpowered_op.py"], None, None
    )

    assert result.verdict is not Verdict.FAIL
    assert "advisory" in result.detail
    _assert_tracked_partition_untouched(real_store_path, before_hash)


def test_end_to_end_unknown_ambient_band_suppresses_fail_to_advisory(monkeypatch, tmp_path):
    """Non-vacuous C4 anchor: an over-budget record with an unknown ambient
    band must NOT go red end-to-end. This is the case that fails with C4
    reverted — unlike a plain over-budget FAIL, which is unaffected by C4
    either way."""
    real_store_path = baseline_store.DEFAULT_STORE_PATH
    before_hash = _hash_file(real_store_path)
    monkeypatch.setattr(baseline_store, "DEFAULT_STORE_PATH", tmp_path / "baseline.jsonl")

    record = _record(
        op="e2e.null_ambient.op",
        verdict_source="fail",
        sample_count=10,
        live_sessions=None,
    )
    baseline_store.append(record)

    monkeypatch.setattr(
        latency_dim,
        "_load_op_inventory",
        lambda: [
            {
                "op_key": "e2e.null_ambient.op",
                "module_path": "coordinator_core/ops/fake_e2e_null_ambient_op.py",
                "classification": "COMPUTE_ONLY",
            }
        ],
    )

    result = latency_dim._check_latency(
        ["coordinator_core/ops/fake_e2e_null_ambient_op.py"], None, None
    )

    assert result.verdict is not Verdict.FAIL
    assert "downgraded from fail" in result.detail
    assert "floor_delta_ms" not in result.detail
    _assert_tracked_partition_untouched(real_store_path, before_hash)


def test_end_to_end_high_load_ambient_band_suppresses_fail_to_advisory(monkeypatch, tmp_path):
    """Non-vacuous C4 anchor: an over-budget record measured above C1's
    high-load band (live_sessions > 45) must NOT go red end-to-end — the
    other case that fails with C4 reverted."""
    real_store_path = baseline_store.DEFAULT_STORE_PATH
    before_hash = _hash_file(real_store_path)
    monkeypatch.setattr(baseline_store, "DEFAULT_STORE_PATH", tmp_path / "baseline.jsonl")

    record = _record(
        op="e2e.high_load.op",
        verdict_source="fail",
        sample_count=10,
        live_sessions=46,
    )
    baseline_store.append(record)

    monkeypatch.setattr(
        latency_dim,
        "_load_op_inventory",
        lambda: [
            {
                "op_key": "e2e.high_load.op",
                "module_path": "coordinator_core/ops/fake_e2e_high_load_op.py",
                "classification": "COMPUTE_ONLY",
            }
        ],
    )

    result = latency_dim._check_latency(
        ["coordinator_core/ops/fake_e2e_high_load_op.py"], None, None
    )

    assert result.verdict is not Verdict.FAIL
    assert "downgraded from fail" in result.detail
    assert "floor_delta_ms" not in result.detail
    _assert_tracked_partition_untouched(real_store_path, before_hash)


# ---------------------------------------------------------------------------
# Safety-property pins (C7 requirements this row must not regress, per
# docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md; C0 fixed the
# ContextVar clobber that had these red — fbc8075a2). Both properties are
# already exercised above (test_self_excluded_paths_never_reach_op_mapping,
# test_only_validator_changed_yields_unavailable_not_gating,
# test_reentrant_call_raises_loudly,
# test_reentrant_call_via_run_dimension_reads_as_error_not_silent_pass); this
# marker documents the intent so a future edit of the read path does not
# drop them silently.
# ---------------------------------------------------------------------------


def test_self_exclusion_and_reentrancy_pins_are_present():
    """Meta-assertion: both C7 safety-property test names this row must not
    regress still exist as callable tests in this module (a cheap tripwire
    against a future accidental deletion during a read-path edit)."""
    module_globals = globals()
    for name in (
        "test_self_excluded_paths_never_reach_op_mapping",
        "test_only_validator_changed_yields_unavailable_not_gating",
        "test_reentrant_call_raises_loudly",
        "test_reentrant_call_via_run_dimension_reads_as_error_not_silent_pass",
    ):
        assert name in module_globals and callable(module_globals[name])
