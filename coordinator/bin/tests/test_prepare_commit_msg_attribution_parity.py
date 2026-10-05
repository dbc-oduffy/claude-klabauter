"""The prepare-commit-msg hook attaches the same constant attribution as the engine."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from coordinator_core.git import commit_trailers

_HOOK = Path(__file__).resolve().parents[1] / "coordinator-prepare-commit-msg.py"


def _load_hook():
    spec = importlib.util.spec_from_file_location("prepare_commit_msg_hook", _HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hook_constant_equals_engine_constant():
    assert _load_hook().ATTRIBUTION_TRAILER_VALUE == commit_trailers.ATTRIBUTION_TRAILER_VALUE


def test_engine_route_attaches_attribution(tmp_path, monkeypatch):
    hook = _load_hook()
    msg = tmp_path / "MSG"
    msg.write_text("fix: x\n", encoding="utf-8")
    monkeypatch.setattr(hook, "_resolve_staged_paths", lambda: [])
    monkeypatch.setattr(hook, "_resolve_operator", lambda _g: "")
    args = hook._engine_trailer_args(str(msg), str(tmp_path / ".git"))
    assert args is not None
    assert f"Co-Authored-By: {commit_trailers.ATTRIBUTION_TRAILER_VALUE}" in args


def test_fallback_route_attaches_attribution(tmp_path, monkeypatch):
    hook = _load_hook()
    msg = tmp_path / "MSG"
    msg.write_text("fix: x\n", encoding="utf-8")
    monkeypatch.setattr(hook, "_resolve_staged_paths", lambda: [])
    monkeypatch.setattr(hook, "_resolve_operator", lambda _g: "")
    monkeypatch.setattr(hook, "_resolve_deliverable_id", lambda *_a: "")
    args = hook._mirrored_trailer_args(str(msg), str(tmp_path / ".git"), "12121212-1212-4121-8121-121212121212")
    assert f"Co-Authored-By: {commit_trailers.ATTRIBUTION_TRAILER_VALUE}" in args
