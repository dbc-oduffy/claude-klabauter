"""
coordinator_core.tests.test_ipc_error_message — the INTERNAL_ERROR envelope carries the
exception's own message text, not just its class name; the op-timeout envelope names the
route (warm or cold) the op ran on.

Spec backlink: docs/plans/2026-08-20-newline-bearing-argv-fails-loud.md § C5 / AC6
"""

from __future__ import annotations

from coordinator_core.ipc import INTERNAL_ERROR, _handler_exception_error, _timeout_error_envelope
from coordinator_core.telemetry import op_latency


def test_internal_error_message_carries_exception_text():
    exc = ValueError("distinctive text naming the offending field")
    error = _handler_exception_error(exc)

    assert error["code"] == INTERNAL_ERROR
    assert "ValueError" in error["message"]
    assert "distinctive text naming the offending field" in error["message"]


def test_timeout_envelope_says_cold_when_not_in_warm_engine(monkeypatch):
    monkeypatch.delenv(op_latency.ROUTE_ENV, raising=False)
    msg = _timeout_error_envelope("ceremony.commit_v2", 30.0, 1)["error"]["message"]

    assert "ran cold" in msg
    assert "inside the warm engine" not in msg
    assert "warm dispatch indeterminate" not in msg


def test_timeout_envelope_says_warm_inside_warm_engine(monkeypatch):
    monkeypatch.setenv(op_latency.ROUTE_ENV, op_latency.WARM_SERVER)
    msg = _timeout_error_envelope("ceremony.commit_v2", 30.0, 1)["error"]["message"]

    assert "ran warm" in msg
    assert "inside the warm engine" in msg
