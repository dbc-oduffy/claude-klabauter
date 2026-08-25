"""
coordinator_core.ops.tests.test_gate_validate_invocable

Op-level tests for "gate.validate_invocable" (C1 skeleton,
docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C1).

Coverage:
  - the op is reachable via the live registry (import-guard)
  - the tri-state verdict shape: five dimensions, fixed order, each carrying
    a real Verdict member (never a bare bool)
  - fail-closed on internal exception: a dimension check that raises is
    reported as ERROR, never PASS, and never silently dropped
  - the dimension seam: register_dimension() lets a caller plug in a real
    check without touching the dispatch core, and rejects an unknown name
  - overall-verdict precedence: ERROR > FAIL > PASS; UNAVAILABLE/SKIPPED
    never flip an already-earned PASS away, but PASS must be earned — an
    all-UNAVAILABLE/SKIPPED run (nothing measured) reports UNAVAILABLE, not
    a vacuous PASS
  - the tests dimension ships SKIPPED(gated), distinct from the other four
    stubs' UNAVAILABLE (Anti-scope: gated on G4, not "tool missing")
  - required-param error on missing changed_files

Spec backlink: docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C1
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.gate_validate_invocable  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.gate_validate_invocable import (
    DIMENSION_NAMES,
    OP_KEY,
    DimensionResult,
    Verdict,
    _DIMENSION_REGISTRY,
    _gate_validate_invocable,
    _overall_verdict,
    _run_dimension,
    register_dimension,
    to_json,
)

assert OP_KEY in _REGISTRY, (
    f"import guard failed: {OP_KEY!r} not in _REGISTRY — "
    "coordinator_core.ops.gate_validate_invocable @register_op did not fire"
)


@pytest.fixture(autouse=True)
def _restore_dimension_registry():
    """Every test gets the module's canonical stub registry, not whatever
    `_DIMENSION_REGISTRY` happens to hold at test start.

    Forced to `canonical_stub_dimension_registry()` rather than a snapshot of
    the live registry: `test_gate_dimension_types.py` and
    `test_gate_dimension_docstrings.py` each replace their slot as an IMPORT
    side effect, and that mutation is permanent for the rest of the pytest
    session once either file is collected -- a snapshot taken here would
    capture the REAL check, not the stub, whenever this file runs after
    either of those in the same session. See
    `_dod_gate_test_helpers.canonical_stub_dimension_registry`'s docstring
    (gap 2) for the full defect this closes."""
    from coordinator_core.ops.tests._dod_gate_test_helpers import (
        canonical_stub_dimension_registry,
    )

    original = dict(_DIMENSION_REGISTRY)
    _DIMENSION_REGISTRY.clear()
    _DIMENSION_REGISTRY.update(canonical_stub_dimension_registry())
    yield
    _DIMENSION_REGISTRY.clear()
    _DIMENSION_REGISTRY.update(original)


# ---------------------------------------------------------------------------
# Tri-state verdict shape
# ---------------------------------------------------------------------------


def test_five_dimensions_in_fixed_order():
    result = _gate_validate_invocable({"changed_files": ["a.py"]})
    assert [d["dimension"] for d in result["dimensions"]] == list(DIMENSION_NAMES)


def test_every_dimension_verdict_is_a_real_verdict_member():
    result = _gate_validate_invocable({"changed_files": ["a.py"]})
    valid = {v.value for v in Verdict}
    for dim in result["dimensions"]:
        assert dim["verdict"] in valid
        assert not isinstance(dim["verdict"], bool)


def test_tests_dimension_is_skipped_gated_not_unavailable():
    result = _gate_validate_invocable({"changed_files": ["a.py"]})
    tests_dim = next(d for d in result["dimensions"] if d["dimension"] == "tests")
    assert tests_dim["verdict"] == "SKIPPED"
    assert "gated_reason" in tests_dim
    assert "G4" in tests_dim["gated_reason"]


def test_other_four_stub_dimensions_are_unavailable():
    result = _gate_validate_invocable({"changed_files": ["a.py"]})
    for name in ("types", "docstrings", "review", "latency"):
        dim = next(d for d in result["dimensions"] if d["dimension"] == name)
        assert dim["verdict"] == "UNAVAILABLE"
        assert "gated_reason" not in dim


def test_result_schema_is_versioned():
    result = _gate_validate_invocable({"changed_files": ["a.py"]})
    assert result["schema_version"] == 1
    assert result["op"] == OP_KEY


def test_default_overall_is_unavailable_with_all_stub_dimensions():
    """All five stub dimensions are UNAVAILABLE/SKIPPED(gated) today — nothing
    actually measured anything, so the honest overall is UNAVAILABLE, not a
    vacuous PASS. This is the regression the fix closes."""
    result = _gate_validate_invocable({"changed_files": ["a.py"]})
    assert result["overall"] == "UNAVAILABLE"


# ---------------------------------------------------------------------------
# Fail-closed on internal exception
# ---------------------------------------------------------------------------


def test_run_dimension_converts_raised_exception_to_error():
    def _boom(changed_files, diff_base, repo_root):
        raise RuntimeError("dimension tooling crashed")

    register_dimension("types", _boom)
    result = _run_dimension("types", ["a.py"], None, None)
    assert result.verdict is Verdict.ERROR
    assert "RuntimeError" in result.detail
    assert "dimension tooling crashed" in result.detail


def test_crashed_dimension_never_reads_as_pass_end_to_end():
    def _boom(changed_files, diff_base, repo_root):
        raise ValueError("boom")

    register_dimension("docstrings", _boom)
    result = _gate_validate_invocable({"changed_files": ["a.py"]})
    docstrings_dim = next(d for d in result["dimensions"] if d["dimension"] == "docstrings")
    assert docstrings_dim["verdict"] == "ERROR"
    assert result["overall"] == "ERROR"


def test_mismatched_dimension_label_in_result_is_also_an_error():
    def _mislabeled(changed_files, diff_base, repo_root):
        return DimensionResult(dimension="review", verdict=Verdict.PASS, detail="wrong label")

    register_dimension("types", _mislabeled)
    result = _run_dimension("types", ["a.py"], None, None)
    assert result.verdict is Verdict.ERROR
    assert "dimension/result name mismatch" in result.detail


# ---------------------------------------------------------------------------
# Dimension seam — register_dimension()
# ---------------------------------------------------------------------------


def test_register_dimension_replaces_a_slot_without_touching_others():
    def _fake_pass(changed_files, diff_base, repo_root):
        return DimensionResult(dimension="review", verdict=Verdict.PASS, detail="stubbed pass")

    register_dimension("review", _fake_pass)
    result = _gate_validate_invocable({"changed_files": ["a.py"]})
    review_dim = next(d for d in result["dimensions"] if d["dimension"] == "review")
    assert review_dim["verdict"] == "PASS"
    # untouched slot still stub UNAVAILABLE
    types_dim = next(d for d in result["dimensions"] if d["dimension"] == "types")
    assert types_dim["verdict"] == "UNAVAILABLE"


def test_register_dimension_rejects_unknown_name():
    def _noop(changed_files, diff_base, repo_root):
        return DimensionResult(dimension="bogus", verdict=Verdict.PASS, detail="")

    with pytest.raises(ValueError, match="unknown dimension"):
        register_dimension("bogus", _noop)


# ---------------------------------------------------------------------------
# Overall-verdict precedence
# ---------------------------------------------------------------------------


def test_overall_verdict_precedence_error_beats_fail_beats_pass():
    pass_r = DimensionResult("a", Verdict.PASS, "")
    fail_r = DimensionResult("b", Verdict.FAIL, "")
    error_r = DimensionResult("c", Verdict.ERROR, "")
    unavailable_r = DimensionResult("d", Verdict.UNAVAILABLE, "")
    skipped_r = DimensionResult("e", Verdict.SKIPPED, "")

    assert _overall_verdict([pass_r, unavailable_r, skipped_r]) is Verdict.PASS
    assert _overall_verdict([pass_r, fail_r, unavailable_r]) is Verdict.FAIL
    assert _overall_verdict([pass_r, fail_r, error_r]) is Verdict.ERROR
    # nothing PASSed and nothing FAILed/ERRORed but something was
    # UNAVAILABLE/SKIPPED — a vacuous PASS is wrong; UNAVAILABLE is honest.
    assert _overall_verdict([unavailable_r, skipped_r]) is Verdict.UNAVAILABLE


def test_overall_verdict_all_unavailable_or_skipped_is_not_pass():
    """Regression test: the bug this fix closes was `overall: PASS` on a run
    where every dimension reported UNAVAILABLE/SKIPPED — i.e. nothing was
    actually measured. That must never read as PASS."""
    unavailable_r = DimensionResult("d", Verdict.UNAVAILABLE, "")
    skipped_r = DimensionResult("e", Verdict.SKIPPED, "")
    result = _overall_verdict([unavailable_r, unavailable_r, unavailable_r, unavailable_r, skipped_r])
    assert result is Verdict.UNAVAILABLE
    assert result is not Verdict.PASS


def test_fail_dimension_flips_overall_to_fail_end_to_end():
    def _fake_fail(changed_files, diff_base, repo_root):
        return DimensionResult(dimension="types", verdict=Verdict.FAIL, detail="stubbed fail")

    register_dimension("types", _fake_fail)
    result = _gate_validate_invocable({"changed_files": ["a.py"]})
    assert result["overall"] == "FAIL"


# ---------------------------------------------------------------------------
# to_json() shape + param validation
# ---------------------------------------------------------------------------


def test_to_json_matches_handler_output_shape():
    results = [_run_dimension(name, [], None, None) for name in DIMENSION_NAMES]
    overall = _overall_verdict(results)
    payload = to_json(overall, results)
    assert set(payload) == {"schema_version", "op", "overall", "dimensions"}
    assert len(payload["dimensions"]) == len(DIMENSION_NAMES)


def test_missing_changed_files_raises_value_error():
    with pytest.raises(ValueError, match="changed_files"):
        _gate_validate_invocable({})


def test_empty_changed_files_list_is_accepted():
    result = _gate_validate_invocable({"changed_files": []})
    assert result["overall"] == "UNAVAILABLE"


def test_diff_base_param_is_accepted_and_not_required():
    result = _gate_validate_invocable({"changed_files": ["a.py"], "diff_base": "origin/main"})
    assert result["overall"] == "UNAVAILABLE"
