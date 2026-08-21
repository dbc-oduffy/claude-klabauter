"""coordinator_core.op_census.tests.test_timing — tests for the process-time
and invocation-tax axes (C4).

Covers: routed-denominator filtering (DR-332 § Item 2), per-op p50/max
process-time aggregation and classification, `AxisResult`'s NO_DATA/reason
invariant, the invocation-tax axis's single-measured-floor shape, and
`cleared_ops`' exhaustive dispatch over the three `Disposition` states.

Spec backlink: state/dispatch-briefs/2026-08-21-the-census-that-cannot-miss-an-op/C4.md
"""

from __future__ import annotations

import pytest

from coordinator_core.op_census.timing import (
    INVOCATION_TAX_BAR_MS,
    PROCESS_TIME_BAR_MS,
    AxisResult,
    Disposition,
    NoDataReason,
    cleared_ops,
    invocation_tax_dispositions,
    process_time_by_op,
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


def test_process_time_by_op_under_bar():
    entries = [_row("op.a", 10.0), _row("op.a", 20.0), _row("op.a", 30.0)]
    result = process_time_by_op(entries, ["op.a"])
    r = result["op.a"]
    assert r.disposition is Disposition.UNDER_BAR
    assert r.p50_ms == 20.0
    assert r.max_ms == 30.0
    assert r.sample_count == 3


def test_process_time_by_op_over_bar_on_max_even_if_p50_holds():
    entries = [_row("op.a", 10.0), _row("op.a", 10.0), _row("op.a", PROCESS_TIME_BAR_MS + 1)]
    result = process_time_by_op(entries, ["op.a"])
    assert result["op.a"].disposition is Disposition.OVER_BAR


def test_process_time_by_op_no_data_for_unobserved_op():
    entries = [_row("op.a", 10.0)]
    result = process_time_by_op(entries, ["op.a", "op.never_run"])
    never = result["op.never_run"]
    assert never.disposition is Disposition.NO_DATA
    assert never.no_data_reason is NoDataReason.NEVER_OBSERVED


def test_process_time_by_op_rotated_generation_reason():
    entries = []
    result = process_time_by_op(entries, ["op.rotated"], rotated_generation_ops=["op.rotated"])
    r = result["op.rotated"]
    assert r.disposition is Disposition.NO_DATA
    assert r.no_data_reason is NoDataReason.NOT_IN_CURRENT_GENERATION


def test_process_time_by_op_ignores_null_route_rows():
    entries = [_row("op.a", 10.0, route=None), _row("op.a", 20.0, route=None)]
    result = process_time_by_op(entries, ["op.a"])
    assert result["op.a"].disposition is Disposition.NO_DATA


def test_process_time_by_op_ignores_started_rows():
    entries = [_row("op.a", 10.0, kind="started"), _row("op.a", 20.0, kind="complete")]
    result = process_time_by_op(entries, ["op.a"])
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
