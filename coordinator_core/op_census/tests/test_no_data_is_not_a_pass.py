"""coordinator_core.op_census.tests.test_no_data_is_not_a_pass — the named
deliverable (C4).

Purpose: fail if a future edit collapses `no_data` into `under_bar`,
anywhere on the path from a raw sample to the EMITTED artifact — not only in
the in-memory `Disposition` enum (staff-eng Finding 7). An enum-only test
would not have caught the incident this guard exists for: a first CPU-time
probe used `os.times()` child fields, which are always `0.0` on Windows, and
reported PASS for a 421ms import. That was a wrong-value PRODUCER, not a
collapsed enum — so this file asserts at BOTH the producer boundary (the
probe script itself) and the emitted-disposition boundary (no op with
`no_data` on any axis ever appears in a cleared/passing set).

Spec backlink: state/dispatch-briefs/2026-08-21-the-census-that-cannot-miss-an-op/C4.md
"""

from __future__ import annotations

import inspect

import pytest

from coordinator_core.op_census import timing
from coordinator_core.op_census.timing import (
    AxisResult,
    Disposition,
    NoDataReason,
    cleared_ops,
    emit_dispositions,
    invocation_tax_dispositions,
    handler_elapsed_by_op,
)


def test_disposition_enum_has_exactly_three_states():
    assert {d.value for d in Disposition} == {"over_bar", "under_bar", "no_data"}


def test_no_data_never_equals_under_bar():
    assert Disposition.NO_DATA is not Disposition.UNDER_BAR
    assert Disposition.NO_DATA.value != Disposition.UNDER_BAR.value


def test_tax_probe_script_never_uses_os_times():
    """Producer-boundary regression guard for the os.times() incident.

    `os.times()` child fields (`children_user`/`children_system`) are
    always `0.0` on Windows — a probe built on them silently reports zero
    process time for any child work, however slow. The fix was having the
    measured child report its OWN `time.process_time()` instead; this pins
    that shape by inspecting the exact script the child executes, not just
    the caller.
    """
    assert "os.times" not in timing._TAX_PROBE_SCRIPT
    assert "time.process_time()" in timing._TAX_PROBE_SCRIPT


def test_measure_invocation_tax_source_never_reads_os_times_children():
    source = inspect.getsource(timing.measure_invocation_tax_ms)
    assert "os.times" not in source


def test_emitted_disposition_excludes_no_data_op_from_cleared():
    """The consequential assertion: NOT the enum, the EMITTED artifact.

    An op with `no_data` on the process-time axis (even with a fully
    passing invocation-tax axis) must never appear in
    `emit_dispositions(...)["cleared"]`.
    """
    handler_elapsed = {
        "op.unmeasured": AxisResult(
            disposition=Disposition.NO_DATA, no_data_reason=NoDataReason.NEVER_OBSERVED
        ),
        "op.measured_good": AxisResult(
            disposition=Disposition.UNDER_BAR, p50_ms=1.0, max_ms=1.0, sample_count=5
        ),
    }
    invocation_tax = {
        "op.unmeasured": AxisResult(disposition=Disposition.UNDER_BAR, max_ms=1.0, sample_count=1),
        "op.measured_good": AxisResult(disposition=Disposition.UNDER_BAR, max_ms=1.0, sample_count=1),
    }

    emitted = emit_dispositions(handler_elapsed, invocation_tax)

    assert "op.unmeasured" not in emitted["cleared"]
    assert "op.measured_good" in emitted["cleared"]
    assert emitted["ops"]["op.unmeasured"]["handler_elapsed"]["disposition"] == "no_data"
    assert emitted["ops"]["op.unmeasured"]["handler_elapsed"]["no_data_reason"] == "never_observed"


def test_emitted_disposition_excludes_no_data_on_either_axis():
    handler_elapsed = {
        "op.a": AxisResult(disposition=Disposition.UNDER_BAR, p50_ms=1.0, max_ms=1.0, sample_count=1),
    }
    invocation_tax = {
        "op.a": AxisResult(disposition=Disposition.NO_DATA, no_data_reason=NoDataReason.NEVER_OBSERVED),
    }
    emitted = emit_dispositions(handler_elapsed, invocation_tax)
    assert "op.a" not in emitted["cleared"]


def test_end_to_end_no_data_op_never_reaches_cleared():
    """Real pipeline: telemetry -> handler_elapsed_by_op -> invocation_tax ->
    emit_dispositions, for an op with zero telemetry rows at all.
    """
    ops = ["op.never_ran"]
    handler_elapsed = handler_elapsed_by_op([], ops)
    invocation_tax = invocation_tax_dispositions(ops, measured_tax_ms=None)

    emitted = emit_dispositions(handler_elapsed, invocation_tax)

    assert emitted["cleared"] == []
    assert emitted["ops"]["op.never_ran"]["handler_elapsed"]["disposition"] == "no_data"
    assert emitted["ops"]["op.never_ran"]["invocation_tax"]["disposition"] == "no_data"


def test_cleared_ops_dispatch_is_exhaustive_no_default_branch():
    """A fourth, unrecognised state must raise, never silently pass.

    Exercises the else-branch `cleared_ops` reaches when a disposition
    value is neither `OVER_BAR`, `UNDER_BAR`, nor `NO_DATA` — the Python
    analogue of a `match` with no default: any state this module's authors
    did not enumerate explicitly fails loudly here.
    """

    class _FourthState:
        disposition = "some_future_state"

    with pytest.raises(RuntimeError):
        cleared_ops(
            {"op.a": _FourthState()},
            {"op.a": AxisResult(disposition=Disposition.UNDER_BAR, max_ms=1.0, sample_count=1)},
        )
