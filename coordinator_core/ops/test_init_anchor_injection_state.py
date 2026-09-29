
"""Unit tests for `coordinator_core.ops.init_anchor_injection_state`: the idempotent anchor-injection-state initializer."""

from __future__ import annotations

import datetime

import pytest

from coordinator_core.ops import init_anchor_injection_state as mod


def test_happy_path(monkeypatch):
    monkeypatch.setattr(mod, "coordinator_content_root", lambda: "/fake/content-root")

    result = mod._handler({})

    assert result["content_root"] == "/fake/content-root"
    assert result["today"] == datetime.date.today().isoformat()
    assert result["injected_dates"] == []
    assert result["content_gap_dates"] == []


def test_double_invocation_is_idempotent(monkeypatch):
    monkeypatch.setattr(mod, "coordinator_content_root", lambda: "/fake/content-root")

    first = mod._handler({})
    second = mod._handler({})

    assert first == second
    assert first["injected_dates"] is not second["injected_dates"]
    assert first["content_gap_dates"] is not second["content_gap_dates"]


def test_unresolvable_content_root_fails_loud(monkeypatch):
    monkeypatch.setattr(mod, "coordinator_content_root", lambda: None)

    with pytest.raises(RuntimeError, match="cannot resolve the coordinator root"):
        mod._handler({})


def test_params_argument_ignored(monkeypatch):
    monkeypatch.setattr(mod, "coordinator_content_root", lambda: "/fake/content-root")

    result = mod._handler({"unexpected": "value"})

    assert result["content_root"] == "/fake/content-root"
