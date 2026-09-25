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


def test_no_suite_ceiling_or_workers_cap_axis_in_module():
    text = pathlib.Path(gate_budget.__file__).read_text()
    assert "fast_tier_suite_process_ceiling_ms" not in text
    assert "workers_cap" not in text
