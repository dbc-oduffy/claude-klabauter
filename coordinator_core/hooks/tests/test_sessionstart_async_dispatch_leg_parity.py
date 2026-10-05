"""Leg parity, source gating and isolation for hooks.sessionstart_async_dispatch."""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core.hooks import sessionstart_async_dispatch as mod

ALL_FIVE = {"startup", "resume", "clear", "compact", "fork"}

# Pinned at coordinator-content-repo@054f62ba51, sessionstart-async-dispatch.py REGISTRY order.
PINNED_LEGS = [
    ("session_start_repair_prepare_commit_msg_hook", {"startup"}),
    ("session_start_register_published_engine", ALL_FIVE),
    ("sessionstart_ensure_http_forwarder", ALL_FIVE),
    ("session_start_write_plugin_root_breadcrumb", ALL_FIVE),
]

_LEG_ATTRS = {
    "session_start_repair_prepare_commit_msg_hook": "_session_start_repair_prepare_commit_msg_hook_handler",
    "session_start_register_published_engine": "_session_start_register_published_engine_handler",
    "sessionstart_ensure_http_forwarder": "_sessionstart_ensure_http_forwarder_handler",
    "session_start_write_plugin_root_breadcrumb": "_session_start_write_plugin_root_breadcrumb_handler",
}


def _run(payload):
    return asyncio.run(mod._handler({"payload": payload}))


@pytest.fixture
def calls(monkeypatch, tmp_path):
    rec: list[str] = []

    def make(key):
        def leg(params):
            rec.append(key)
            return {}
        return leg

    for key, attr in _LEG_ATTRS.items():
        monkeypatch.setattr(mod, attr, make(key))
    return rec


def test_pinned_leg_list_and_sources():
    assert [(k, set(s)) for k, s, _ in mod._LEGS] == PINNED_LEGS


@pytest.mark.parametrize("source", sorted(ALL_FIVE))
def test_source_gate_matrix(calls, tmp_path, source):
    _run({"source": source, "cwd": str(tmp_path)})
    assert calls == [k for k, s in PINNED_LEGS if source in s]


def test_startup_only_leg_runs_on_startup_not_resume(calls, tmp_path):
    _run({"source": "startup", "cwd": str(tmp_path)})
    assert "session_start_repair_prepare_commit_msg_hook" in calls
    calls.clear()
    _run({"source": "resume", "cwd": str(tmp_path)})
    assert "session_start_repair_prepare_commit_msg_hook" not in calls


def test_missing_source_runs_nothing(calls, tmp_path):
    assert _run({"cwd": str(tmp_path)}) == {}
    assert calls == []


def test_unmatched_source_breadcrumb(calls, tmp_path):
    out = _run({"source": "bogus", "cwd": str(tmp_path)})
    assert calls == []
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "source='bogus' matches no guard in REGISTRY" in ctx


def test_raising_leg_does_not_drop_others(calls, monkeypatch, tmp_path):
    def boom(params):
        raise RuntimeError("x")

    monkeypatch.setattr(mod, "_session_start_register_published_engine_handler", boom)
    _run({"source": "startup", "cwd": str(tmp_path)})
    assert "session_start_register_published_engine" not in calls
    assert calls[-1] == "session_start_write_plugin_root_breadcrumb"
    assert len(calls) == 3
