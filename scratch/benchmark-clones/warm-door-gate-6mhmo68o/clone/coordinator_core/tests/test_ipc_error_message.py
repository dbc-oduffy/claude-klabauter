"""
coordinator_core.tests.test_ipc_error_message — the INTERNAL_ERROR envelope carries the
exception's own message text, not just its class name.

Spec backlink: docs/plans/2026-08-20-newline-bearing-argv-fails-loud.md § C5 / AC6
"""

from __future__ import annotations

from coordinator_core.ipc import INTERNAL_ERROR, _handler_exception_error


def test_internal_error_message_carries_exception_text():
    exc = ValueError("distinctive text naming the offending field")
    error = _handler_exception_error(exc)

    assert error["code"] == INTERNAL_ERROR
    assert "ValueError" in error["message"]
    assert "distinctive text naming the offending field" in error["message"]
