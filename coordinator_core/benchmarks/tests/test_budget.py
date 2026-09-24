"""Unit tests for coordinator_core.benchmarks.budget (two-level budget resolver).

Covers AC5: per-op override wins over the OpClass-tier default. Of the two
manifest-supplied defaults, `MUTATING` still carries `_provisional: true`
(unmeasured, Phase-0 placeholder discipline); `COMPUTE_ONLY` was measured by
the C9 Phase-0 pass and does not. `HOOK_PORT` (the per-tool-call hook tier,
added by docs/plans/2026-09-23-hook-port-budget-tier-and-ehms-04-reconciliation.md
§ C1) also carries `_provisional: true` -- see budget.py's module docstring
for what each marker does and does not mean.

Spec backlink: pln-qsub-01-per-op-end-to-end-late-53ff10 § C8 (AC5).
Spec backlink: docs/plans/2026-09-23-hook-port-budget-tier-and-ehms-04-reconciliation.md § C1.
"""

from __future__ import annotations

from coordinator_core.authz.classification import OpClass
from coordinator_core.benchmarks import op_fixtures
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
    """No override carries `_provisional`. An override is either authored by
    C9's Phase-0 measurement, or -- per `budget.py`'s module docstring, dated
    2026-09-24 -- declares shape-only timing in its `_rationale` (its
    `target_ms`/`tolerance` copy the tier default only to satisfy
    `_validated_budget`'s required shape; its `_rationale` says so). Neither
    class is `_provisional`. Asserts the absence (the negative case Slice-A
    F7 flagged), not just the positive one."""
    manifest = load_manifest()
    for op_name, entry in manifest.get("overrides", {}).items():
        assert "_provisional" not in entry, (
            f"override for op {op_name!r} must not be _provisional"
        )


def test_resolve_budget_loads_real_manifest_when_none_passed():
    # coverage.gate resolves to the (measured) COMPUTE_ONLY tier default.
    budget = resolve_budget("coverage.gate", OpClass.COMPUTE_ONLY)
    assert "target_ms" in budget and "tolerance" in budget
    assert "_provisional" not in budget


def test_hook_port_defaults_verbatim():
    """AC1/AC2: HOOK_PORT is present, reached by name, and equals the plan's verbatim block."""
    manifest = load_manifest()
    assert sorted(manifest["defaults"]) == ["COMPUTE_ONLY", "HOOK_PORT", "MUTATING"]
    hook_port = manifest["defaults"]["HOOK_PORT"]
    assert hook_port["target_ms"] == 15
    assert hook_port["tolerance"] == {"kind": "relative", "value": 0.2}
    assert hook_port["_provisional"] is True

    budget = resolve_budget("hooks.session_heartbeat", "HOOK_PORT", manifest=manifest)
    assert budget["target_ms"] == 15
    assert budget["tolerance"] == {"kind": "relative", "value": 0.2}


def test_compute_only_hook_op_unaffected_by_hook_port():
    """AC3: a representative hooks.* op still resolves to the unchanged COMPUTE_ONLY default
    when passed OpClass.COMPUTE_ONLY, rather than being pulled toward HOOK_PORT."""
    op = "hooks.nudge_unauthorized_handoff"
    assert op in op_fixtures.COMPUTE_ONLY_FIXTURES
    manifest = load_manifest()
    budget = resolve_budget(op, OpClass.COMPUTE_ONLY, manifest=manifest)
    assert budget == manifest["defaults"]["COMPUTE_ONLY"]
    assert budget["target_ms"] == 70
    assert "_provisional" not in budget


def test_hook_port_band_sits_below_measured_cold_floor():
    """AC4: HOOK_PORT's tolerance band must sit strictly below the manifest's own
    committed cold-bare-interpreter floor, never a hard-coded figure."""
    manifest = load_manifest()
    cold_floor_ms = manifest["_hook_seam_http_transport"]["reverification"][
        "cold_bare_interpreter_ms"
    ]["min"]
    hook_port = manifest["defaults"]["HOOK_PORT"]
    band_ms = hook_port["target_ms"] * (1 + hook_port["tolerance"]["value"])
    assert band_ms < cold_floor_ms


def test_no_tier_blind_override_for_a_hooks_op():
    """AC10: no `hooks.*` op may appear in manifest["overrides"] unless that entry
    documents itself as tier-aware. Today this passes vacuously (0 such overrides,
    per census 4/AC3) -- it exists to fail loud the day one shadows HOOK_PORT silently."""
    manifest = load_manifest()
    for op_name, entry in manifest.get("overrides", {}).items():
        if op_name.startswith("hooks."):
            assert "_tier" in entry, (
                f"override for hooks.* op {op_name!r} is tier-blind and would silently "
                "shadow HOOK_PORT -- document it with a `_tier` key or remove it"
            )
