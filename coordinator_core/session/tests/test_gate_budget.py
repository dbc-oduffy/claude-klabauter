"""coordinator_core/session/tests/test_gate_budget.py

Spec: docs/plans/2026-09-07-fix-the-validate-gate-recursive-tier-invocation.md
(B1, AC10). No spawn -- pure-function coverage of gate_budget's formatting
and breach-line thresholds.
"""
from __future__ import annotations

import pathlib

from coordinator_core.session import gate_budget


def test_line_format_and_suffix():
    line = gate_budget.format_budget_line(123, 456)
    assert line == (
        "[validate-gate] self_process_ms=123 suite_process_ms=456 "
        "(process time, not wall clock)"
    )


def test_unreadable_self_reports_unavailable():
    line = gate_budget.format_budget_line(None, 456)
    assert "self_process_ms=unavailable" in line
    assert "suite_process_ms=456" in line


def test_unreadable_suite_reports_unavailable():
    line = gate_budget.format_budget_line(123, None)
    assert "self_process_ms=123" in line
    assert "suite_process_ms=unavailable" in line


def test_both_unreadable():
    line = gate_budget.format_budget_line(None, None)
    assert "self_process_ms=unavailable" in line
    assert "suite_process_ms=unavailable" in line


def test_501_breaches():
    breach = gate_budget.format_breach_line(501)
    assert breach is not None
    assert "DR-344" in breach
    assert "501" in breach


def test_500_does_not_breach():
    assert gate_budget.format_breach_line(500) is None


def test_unreadable_self_never_breaches():
    assert gate_budget.format_breach_line(None) is None


def test_posix_delta(monkeypatch):
    monkeypatch.setattr(gate_budget.os, "name", "posix")
    before = (1.0, 0.5)
    after = (1.25, 0.6)
    # (1.25-1.0) + (0.6-0.5) = 0.35s -> 350ms
    assert gate_budget.suite_process_ms(before=before, after=after) == 350


def test_posix_missing_snapshot_is_unavailable(monkeypatch):
    monkeypatch.setattr(gate_budget.os, "name", "posix")
    assert gate_budget.suite_process_ms(before=None, after=(1.0, 1.0)) is None
    assert gate_budget.suite_process_ms(before=(1.0, 1.0), after=None) is None


def test_windows_no_job_handle_is_unavailable(monkeypatch):
    monkeypatch.setattr(gate_budget.os, "name", "nt")
    assert gate_budget.suite_process_ms(job_handle=None) is None


def test_windows_unreadable_job_object_is_unavailable(monkeypatch):
    monkeypatch.setattr(gate_budget.os, "name", "nt")
    # A non-None, non-ctypes-compatible handle -- the ctypes call inside
    # _windows_job_process_ms raises, and that failure must be swallowed
    # into "unavailable", never propagated.
    assert gate_budget.suite_process_ms(job_handle=object()) is None


def test_self_process_ms_returns_a_non_negative_int_or_none():
    value = gate_budget.self_process_ms()
    assert value is None or (isinstance(value, int) and value >= 0)


def test_self_process_ms_unreadable_counter_is_unavailable(monkeypatch):
    def _boom():
        raise OSError("boom")

    monkeypatch.setattr(gate_budget.os, "times", _boom)
    assert gate_budget.self_process_ms() is None


def _patch_span(monkeypatch, fn):
    from coordinator_core.telemetry import op_latency

    monkeypatch.setattr(op_latency, "record_fact_span", fn)


def test_breach_records_one_span_and_one_stderr_line(monkeypatch, capsys):
    calls = []
    _patch_span(monkeypatch, lambda **kw: calls.append(kw))
    gate_budget.emit_budget_lines(501, 10)
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1
    assert "BREACH" not in err
    assert len(calls) == 1
    assert calls[0]["process_ms"] == 501
    assert calls[0]["fact"] == "validate_gate.self_process"


def test_500_records_nothing(monkeypatch, capsys):
    calls = []
    _patch_span(monkeypatch, lambda **kw: calls.append(kw))
    gate_budget.emit_budget_lines(500, 10)
    assert calls == []
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_raising_sink_is_swallowed(monkeypatch, capsys):
    def _boom(**kw):
        raise OSError("sink down")

    _patch_span(monkeypatch, _boom)
    gate_budget.emit_budget_lines(900, 10)
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1
    assert "self_process_ms=900" in err


def test_no_suite_ceiling_or_workers_cap_axis_in_module():
    text = pathlib.Path(gate_budget.__file__).read_text()
    assert "fast_tier_suite_process_ceiling_ms" not in text
    assert "workers_cap" not in text
