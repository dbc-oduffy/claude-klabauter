"""
Tests for coordinator_core.ops.init_anchor_injection_state.

Coverage:
    (a) Happy path — resolved doe_root, ISO today, empty accumulator lists.
    (b) Idempotency (AC7) — two back-to-back invocations with identical
        params ({}) return equivalent output.
    (c) Unresolvable doe_root fails loud (RuntimeError), never a silent
        empty-string placeholder.
"""

from __future__ import annotations

import datetime

import pytest

from coordinator_core.ops import init_anchor_injection_state as mod


def test_happy_path(monkeypatch):
    monkeypatch.setattr(mod, "coordinator_doe_root", lambda: "/fake/doe-root")

    result = mod._handler({})

    assert result["doe_root"] == "/fake/doe-root"
    assert result["today"] == datetime.date.today().isoformat()
    assert result["injected_dates"] == []
    assert result["content_gap_dates"] == []


def test_double_invocation_is_idempotent(monkeypatch):
    monkeypatch.setattr(mod, "coordinator_doe_root", lambda: "/fake/doe-root")

    first = mod._handler({})
    second = mod._handler({})

    assert first == second
    # Distinct list objects, no shared/mutated accumulator between calls.
    assert first["injected_dates"] is not second["injected_dates"]
    assert first["content_gap_dates"] is not second["content_gap_dates"]


def test_unresolvable_doe_root_fails_loud(monkeypatch):
    monkeypatch.setattr(mod, "coordinator_doe_root", lambda: None)

    with pytest.raises(RuntimeError, match="cannot resolve the coordinator root"):
        mod._handler({})


def test_params_argument_ignored(monkeypatch):
    monkeypatch.setattr(mod, "coordinator_doe_root", lambda: "/fake/doe-root")

    result = mod._handler({"unexpected": "value"})

    assert result["doe_root"] == "/fake/doe-root"
