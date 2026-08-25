"""coordinator_core.op_census.tests.test_timing — tests for the process-time
and invocation-tax axes (C4).

Covers: routed-denominator filtering (DR-332 § Item 2), per-op p50/max
process-time aggregation and classification, `AxisResult`'s NO_DATA/reason
invariant, the invocation-tax axis's single-measured-floor shape, and
`cleared_ops`' exhaustive dispatch over the three `Disposition` states.

Spec backlink: state/dispatch-briefs/2026-08-21-the-census-that-cannot-miss-an-op/C4.md
"""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.op_census.timing import (
    INVOCATION_TAX_BAR_MS,
    PROCESS_TIME_BAR_MS,
    AxisResult,
    Disposition,
    NoDataReason,
    UniformInvocationTaxError,
    cleared_ops,
    emit_dispositions,
    invocation_tax_dispositions,
    handler_elapsed_by_op,
    measure_invocation_tax_ms,
    routed_entries,
)


def _row(op, elapsed_ms, route="in_process", kind="complete"):
    return {"op": op, "elapsed_ms": elapsed_ms, "route": route, "kind": kind}


def test_routed_entries_excludes_null_and_missing_route():
    entries = [
        _row("a", 10.0, route="in_process"),
        _row("b", 10.0, route=None),
        {"op": "c", "elapsed_ms": 10.0},  # no route key at all
        _row("d", 10.0, route="warm_server"),
        _row("e", 10.0, route="bogus_route"),
    ]
    routed = routed_entries(entries)
    assert {e["op"] for e in routed} == {"a", "d"}


def test_axis_result_requires_reason_for_no_data():
    with pytest.raises(ValueError):
        AxisResult(disposition=Disposition.NO_DATA)


def test_axis_result_forbids_reason_outside_no_data():
    with pytest.raises(ValueError):
        AxisResult(disposition=Disposition.UNDER_BAR, no_data_reason=NoDataReason.NEVER_OBSERVED)


def test_handler_elapsed_by_op_under_bar():
    entries = [_row("op.a", 10.0), _row("op.a", 20.0), _row("op.a", 30.0)]
    result = handler_elapsed_by_op(entries, ["op.a"])
    r = result["op.a"]
    assert r.disposition is Disposition.UNDER_BAR
    assert r.p50_ms == 20.0
    assert r.max_ms == 30.0
    assert r.sample_count == 3


def test_handler_elapsed_by_op_breach_on_max_reports_not_established_never_over_bar():
    """`elapsed_ms` is wall clock (module docstring) -- a breach on either
    p50 or max is reported as NO_DATA/NOT_ESTABLISHED_UNDER_LOAD, never
    OVER_BAR, because peer load alone can produce it and this axis cannot
    tell the two apart."""
    entries = [_row("op.a", 10.0), _row("op.a", 10.0), _row("op.a", PROCESS_TIME_BAR_MS + 1)]
    result = handler_elapsed_by_op(entries, ["op.a"])
    r = result["op.a"]
    assert r.disposition is Disposition.NO_DATA
    assert r.no_data_reason is NoDataReason.NOT_ESTABLISHED_UNDER_LOAD


def test_handler_elapsed_by_op_no_data_for_unobserved_op():
    entries = [_row("op.a", 10.0)]
    result = handler_elapsed_by_op(entries, ["op.a", "op.never_run"])
    never = result["op.never_run"]
    assert never.disposition is Disposition.NO_DATA
    assert never.no_data_reason is NoDataReason.NEVER_OBSERVED


def test_handler_elapsed_by_op_rotated_generation_reason():
    entries = []
    result = handler_elapsed_by_op(entries, ["op.rotated"], rotated_generation_ops=["op.rotated"])
    r = result["op.rotated"]
    assert r.disposition is Disposition.NO_DATA
    assert r.no_data_reason is NoDataReason.NOT_IN_CURRENT_GENERATION


def test_handler_elapsed_by_op_ignores_null_route_rows():
    entries = [_row("op.a", 10.0, route=None), _row("op.a", 20.0, route=None)]
    result = handler_elapsed_by_op(entries, ["op.a"])
    assert result["op.a"].disposition is Disposition.NO_DATA


def test_handler_elapsed_by_op_ignores_started_rows():
    entries = [_row("op.a", 10.0, kind="started"), _row("op.a", 20.0, kind="complete")]
    result = handler_elapsed_by_op(entries, ["op.a"])
    assert result["op.a"].sample_count == 1
    assert result["op.a"].p50_ms == 20.0


def test_invocation_tax_dispositions_under_bar():
    result = invocation_tax_dispositions(["op.a", "op.b"], measured_tax_ms=10.0)
    assert result["op.a"].disposition is Disposition.UNDER_BAR
    assert result["op.b"].disposition is Disposition.UNDER_BAR
    assert result["op.a"].max_ms == 10.0


def test_invocation_tax_dispositions_over_bar():
    result = invocation_tax_dispositions(["op.a"], measured_tax_ms=INVOCATION_TAX_BAR_MS + 5)
    assert result["op.a"].disposition is Disposition.OVER_BAR


def test_invocation_tax_dispositions_none_is_no_data():
    result = invocation_tax_dispositions(["op.a"], measured_tax_ms=None)
    assert result["op.a"].disposition is Disposition.NO_DATA
    assert result["op.a"].no_data_reason is NoDataReason.NEVER_OBSERVED


def test_invocation_tax_dispositions_nan_is_no_data():
    result = invocation_tax_dispositions(["op.a"], measured_tax_ms=float("nan"))
    assert result["op.a"].disposition is Disposition.NO_DATA


def test_cleared_ops_requires_under_bar_on_both_axes():
    process_time = {
        "op.good": AxisResult(disposition=Disposition.UNDER_BAR, p50_ms=1.0, max_ms=1.0, sample_count=1),
        "op.slow": AxisResult(disposition=Disposition.OVER_BAR, p50_ms=999.0, max_ms=999.0, sample_count=1),
    }
    invocation_tax = {
        "op.good": AxisResult(disposition=Disposition.UNDER_BAR, max_ms=1.0, sample_count=1),
        "op.slow": AxisResult(disposition=Disposition.UNDER_BAR, max_ms=1.0, sample_count=1),
    }
    assert cleared_ops(process_time, invocation_tax) == {"op.good"}


def test_cleared_ops_excludes_no_data():
    process_time = {
        "op.unmeasured": AxisResult(disposition=Disposition.NO_DATA, no_data_reason=NoDataReason.NEVER_OBSERVED),
    }
    invocation_tax = {
        "op.unmeasured": AxisResult(disposition=Disposition.UNDER_BAR, max_ms=1.0, sample_count=1),
    }
    assert cleared_ops(process_time, invocation_tax) == set()


def test_cleared_ops_excludes_op_missing_from_either_axis():
    process_time = {
        "op.a": AxisResult(disposition=Disposition.UNDER_BAR, p50_ms=1.0, max_ms=1.0, sample_count=1),
    }
    invocation_tax: dict = {}
    assert cleared_ops(process_time, invocation_tax) == set()


class _FakeAxisResult:
    """Stand-in used only to prove `cleared_ops` has no default branch.

    A real `AxisResult` can never hold an unrecognised disposition (its
    field is typed `Disposition`), so this duck-types the one attribute
    `cleared_ops` reads to exercise the else-branch directly.
    """

    def __init__(self, disposition):
        self.disposition = disposition


def test_cleared_ops_raises_on_unrecognised_disposition():
    process_time = {"op.a": _FakeAxisResult("not_a_real_state")}
    invocation_tax = {"op.a": AxisResult(disposition=Disposition.UNDER_BAR, max_ms=1.0, sample_count=1)}
    with pytest.raises(RuntimeError):
        cleared_ops(process_time, invocation_tax)


# ---------------------------------------------------------------------------
# measure_invocation_tax_ms shape -- the trampoline cold path, never a bare
# interpreter (2026-08-23 fix, module docstring's CORRECTED block).
# ---------------------------------------------------------------------------


class _FakeCompletedProcess:
    def __init__(self, stdout):
        self.stdout = stdout


def test_measure_invocation_tax_ms_never_passes_dash_S(monkeypatch):
    """Nothing in production disables `site` -- `-S` inflated the old
    probe's module count and is not a shape anything runs in."""
    seen_argv = []

    def _fake_run(argv, **kwargs):
        seen_argv.append(argv)
        payload = {"process_time_ms": 5.0, "module_count": 99, "canary_op_imported": False}
        return _FakeCompletedProcess(json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", _fake_run)
    measure_invocation_tax_ms(iterations=2)
    assert seen_argv, "subprocess.run was never called"
    for argv in seen_argv:
        assert "-S" not in argv


def test_measure_invocation_tax_ms_raises_when_child_did_not_arm(monkeypatch):
    """A child that reports its canary op module WAS imported means op
    registration was not lazy in that child. This must be proven per sample,
    never assumed -- an eager sample silently reverts to the bare-interpreter
    shape this rewrite exists to stop measuring."""

    def _fake_run(argv, **kwargs):
        payload = {"process_time_ms": 400.0, "module_count": 600, "canary_op_imported": True}
        return _FakeCompletedProcess(json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", _fake_run)
    with pytest.raises(RuntimeError):
        measure_invocation_tax_ms(iterations=1)


def test_measure_invocation_tax_ms_averages_armed_samples(monkeypatch):
    calls = iter([10.0, 20.0, 0.0])

    def _fake_run(argv, **kwargs):
        payload = {"process_time_ms": next(calls), "module_count": 99, "canary_op_imported": False}
        return _FakeCompletedProcess(json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert measure_invocation_tax_ms(iterations=3) == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# THE regression: the exact failure the bug row named, now detected rather
# than silently emitted -- every op OVER_BAR on tax must never again produce
# a silent, permanently-empty `cleared` set (bug row
# `state/bug-backlog/2026-08-23-op-census-can-never-clear-an-op-invocation-tax-measured-in-the-wrong-shape.yaml`).
# ---------------------------------------------------------------------------


def test_emit_dispositions_raises_when_tax_uniformly_over_bar_across_every_op():
    """A perfect handler_elapsed axis plus a uniformly-OVER_BAR tax axis
    used to return `cleared: set()` silently -- the exact defect shape.
    `emit_dispositions` must now refuse to emit rather than reporting an
    empty cleared set as though it were a real finding."""
    handler_elapsed = {
        "op.a": AxisResult(disposition=Disposition.UNDER_BAR, p50_ms=1.0, max_ms=1.0, sample_count=5),
        "op.b": AxisResult(disposition=Disposition.UNDER_BAR, p50_ms=1.0, max_ms=1.0, sample_count=5),
    }
    invocation_tax = invocation_tax_dispositions(["op.a", "op.b"], measured_tax_ms=INVOCATION_TAX_BAR_MS + 1)

    with pytest.raises(UniformInvocationTaxError):
        emit_dispositions(handler_elapsed, invocation_tax)


def test_emit_dispositions_does_not_raise_when_tax_uniformly_under_bar():
    """Uniform `UNDER_BAR` is what a healthy measurement looks like by this
    axis's own single-floor design (module docstring) -- not the failure
    signature the guard exists for."""
    handler_elapsed = {
        "op.a": AxisResult(disposition=Disposition.UNDER_BAR, p50_ms=1.0, max_ms=1.0, sample_count=5),
    }
    invocation_tax = invocation_tax_dispositions(["op.a"], measured_tax_ms=1.0)
    emitted = emit_dispositions(handler_elapsed, invocation_tax)
    assert emitted["cleared"] == ["op.a"]


def test_emit_dispositions_does_not_raise_on_mixed_dispositions():
    handler_elapsed = {
        "op.a": AxisResult(disposition=Disposition.UNDER_BAR, p50_ms=1.0, max_ms=1.0, sample_count=1),
        "op.b": AxisResult(disposition=Disposition.UNDER_BAR, p50_ms=1.0, max_ms=1.0, sample_count=1),
    }
    invocation_tax = {
        "op.a": AxisResult(disposition=Disposition.OVER_BAR, max_ms=999.0, sample_count=1),
        "op.b": AxisResult(disposition=Disposition.UNDER_BAR, max_ms=1.0, sample_count=1),
    }
    emitted = emit_dispositions(handler_elapsed, invocation_tax)
    assert emitted["cleared"] == ["op.b"]
