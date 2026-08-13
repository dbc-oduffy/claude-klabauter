"""Unit tests for coordinator_core.benchmarks.record (ConformanceRecord / Tolerance).

Covers AC2: to_json()/from_json() round-trip carries every AC2-pinned field,
and schema_version is pinned to 1.

Spec backlink: pln-qsub-01-per-op-end-to-end-late-53ff10 § C8 (AC2).
"""

from __future__ import annotations

import dataclasses

from coordinator_core.benchmarks.record import SCHEMA_VERSION, ConformanceRecord, Tolerance


def _make_record(**overrides) -> ConformanceRecord:
    fields = dict(
        op="coverage.gate",
        op_class="COMPUTE_ONLY",
        target_ms=150.0,
        tolerance=Tolerance(kind="relative", value=0.2),
        gating_statistic="min",
        gating_statistic_value=42.5,
        min=42.5,
        p50=48.0,
        p95=55.0,
        p99=60.0,
        sample_count=20,
        cold_start_floor_ms=59.0,
        floor_delta_ms=-16.5,
        floor_cov=0.05,
        floor_scope="run",
        run_id="run-abc123",
        verdict="pass",
        baseline_id="baseline-xyz",
        code_sha="a" * 40,
        timestamp="2026-07-10T00:00:00Z",
        runner_isolation_mode="shared",
        schema_version=SCHEMA_VERSION,
    )
    fields.update(overrides)
    return ConformanceRecord(**fields)


def test_schema_version_pinned_to_1():
    assert SCHEMA_VERSION == 1
    record = _make_record()
    assert record.schema_version == 1


def test_to_json_from_json_round_trip_preserves_all_ac2_fields():
    original = _make_record()
    payload = original.to_json()
    restored = ConformanceRecord.from_json(payload)
    assert restored == original


def test_round_trip_preserves_every_dataclass_field_individually():
    """Enumerate every field on the dataclass (not just equality on the whole
    object) so a future field-set change to record.py surfaces here."""
    original = _make_record()
    payload = original.to_json()
    restored = ConformanceRecord.from_json(payload)

    for f in dataclasses.fields(ConformanceRecord):
        original_value = getattr(original, f.name)
        restored_value = getattr(restored, f.name)
        assert restored_value == original_value, (
            f"field {f.name!r} did not round-trip: "
            f"{original_value!r} != {restored_value!r}"
        )


def test_round_trip_preserves_nullable_floor_delta_ms_none():
    original = _make_record(floor_delta_ms=None)
    payload = original.to_json()
    restored = ConformanceRecord.from_json(payload)
    assert restored.floor_delta_ms is None


def test_to_json_serializes_tolerance_as_nested_object():
    record = _make_record()
    payload = record.to_json()
    import json

    data = json.loads(payload)
    assert data["tolerance"] == {"kind": "relative", "value": 0.2}


def test_from_json_reconstructs_tolerance_as_dataclass_instance():
    record = _make_record()
    restored = ConformanceRecord.from_json(record.to_json())
    assert isinstance(restored.tolerance, Tolerance)
    assert restored.tolerance.kind == "relative"
    assert restored.tolerance.value == 0.2


def test_ac2_field_set_is_complete():
    """Pin the exact set of dataclass field names -- guards against silent
    field drops/renames on this qsub-01<->qsub-03 contract surface."""
    expected_fields = {
        "op",
        "op_class",
        "target_ms",
        "tolerance",
        "gating_statistic",
        "gating_statistic_value",
        "min",
        "p50",
        "p95",
        "p99",
        "sample_count",
        "cold_start_floor_ms",
        "floor_delta_ms",
        "floor_cov",
        "floor_scope",
        "run_id",
        "verdict",
        "baseline_id",
        "code_sha",
        "timestamp",
        "runner_isolation_mode",
        "schema_version",
    }
    actual_fields = {f.name for f in dataclasses.fields(ConformanceRecord)}
    assert actual_fields == expected_fields


def test_default_floor_scope_is_run():
    record = _make_record()
    assert record.floor_scope == "run"


def test_default_verdict_is_advisory_when_unset():
    fields = dict(
        op="ping",
        op_class="COMPUTE_ONLY",
        target_ms=50.0,
        tolerance=Tolerance(kind="absolute", value=10.0),
        gating_statistic="min",
        gating_statistic_value=5.0,
        min=5.0,
        p50=5.5,
        p95=6.0,
        p99=6.2,
        sample_count=2,
        cold_start_floor_ms=59.0,
        floor_delta_ms=-53.0,
        floor_cov=0.04,
    )
    record = ConformanceRecord(**fields)
    assert record.verdict == "advisory"
