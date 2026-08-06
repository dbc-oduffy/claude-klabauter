"""Unit tests for coordinator_core.benchmarks.budget (two-level budget resolver).

Covers AC5: per-op override wins over the OpClass-tier default, and every
manifest-supplied default in wave-1's budget-manifest.json carries
`_provisional: true` (Phase-0 placeholder discipline -- see budget.py's
module docstring).

Spec backlink: docs/plans/2026-07-10-qsub-01-latency-benchmark-harness.md § C8 (AC5).
"""

from __future__ import annotations

from coordinator_core.authz.classification import OpClass
from coordinator_core.benchmarks.budget import load_manifest, resolve_budget


def _manifest_with_override():
    return {
        "schema_version": 1,
        "min_gating_sample_count": 5,
        "defaults": {
            "COMPUTE_ONLY": {
                "target_ms": 150,
                "tolerance": {"kind": "relative", "value": 0.2},
                "_provisional": True,
            },
            "MUTATING": {
                "target_ms": 300,
                "tolerance": {"kind": "relative", "value": 0.2},
                "_provisional": True,
            },
        },
        "overrides": {
            "ping": {
                "target_ms": 50,
                "tolerance": {"kind": "absolute", "value": 10},
                "_provisional": True,
            }
        },
    }


def test_override_wins_over_default_for_matching_op():
    manifest = _manifest_with_override()
    budget = resolve_budget("ping", OpClass.COMPUTE_ONLY, manifest=manifest)
    assert budget == manifest["overrides"]["ping"]
    assert budget["target_ms"] == 50


def test_falls_back_to_tier_default_when_no_override():
    manifest = _manifest_with_override()
    budget = resolve_budget("coverage.gate", OpClass.COMPUTE_ONLY, manifest=manifest)
    assert budget == manifest["defaults"]["COMPUTE_ONLY"]
    assert budget["target_ms"] == 150


def test_falls_back_to_mutating_tier_default():
    manifest = _manifest_with_override()
    budget = resolve_budget("some.mutating.op", OpClass.MUTATING, manifest=manifest)
    assert budget == manifest["defaults"]["MUTATING"]
    assert budget["target_ms"] == 300


def test_op_class_accepts_string_tier_name():
    manifest = _manifest_with_override()
    budget = resolve_budget("coverage.gate", "COMPUTE_ONLY", manifest=manifest)
    assert budget == manifest["defaults"]["COMPUTE_ONLY"]


def test_unknown_tier_raises_keyerror():
    manifest = _manifest_with_override()
    try:
        resolve_budget("some.op", "NOT_A_TIER", manifest=manifest)
    except KeyError:
        # Expected -- the else-branch below is the actual assertion.
        pass
    else:
        raise AssertionError("expected KeyError for unresolvable op_class tier")


def test_load_manifest_reads_real_manifest_from_disk():
    manifest = load_manifest()
    assert manifest["schema_version"] == 1
    assert "COMPUTE_ONLY" in manifest["defaults"]
    assert "MUTATING" in manifest["defaults"]


def test_provisional_flags_match_phase0_measurement_state():
    """Phase-0 discipline (post-C9): the `_provisional` marker records whether a
    tier default was measured. COMPUTE_ONLY was measured by the C9 Phase-0 pass, so
    its default is honest (no `_provisional`); MUTATING is unmeasured in wave-1
    (AC7 define-only), so its default MUST still carry `_provisional: true`."""
    manifest = load_manifest()
    assert manifest["defaults"]["MUTATING"].get("_provisional") is True, (
        "MUTATING default is unmeasured in wave-1 and must stay _provisional:true"
    )
    assert "_provisional" not in manifest["defaults"]["COMPUTE_ONLY"], (
        "COMPUTE_ONLY default is measured (C9 Phase-0) and must NOT carry _provisional"
    )


def test_measured_overrides_carry_no_provisional_marker():
    """Per-op overrides are authored by C9's Phase-0 measurement, so any override
    present is measured-honest and must NOT carry `_provisional`. Asserts the
    absence (the negative case Slice-A F7 flagged), not just the positive one."""
    manifest = load_manifest()
    for op_name, entry in manifest.get("overrides", {}).items():
        assert "_provisional" not in entry, (
            f"override for op {op_name!r} is measured and must not be _provisional"
        )


def test_resolve_budget_loads_real_manifest_when_none_passed():
    # coverage.gate resolves to the (measured) COMPUTE_ONLY tier default.
    budget = resolve_budget("coverage.gate", OpClass.COMPUTE_ONLY)
    assert "target_ms" in budget and "tolerance" in budget
    assert "_provisional" not in budget
