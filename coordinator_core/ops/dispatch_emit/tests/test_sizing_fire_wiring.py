"""Wiring of `dispatch.emit --ask`: raw prompt and sizing entries, exclusivity, default output, zero spawns.

`compose_ask_script` is stubbed; its own tests live in test_ask_compose.py.
"""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, op
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    calls: dict = {}

    def fake_compose(**kw):
        calls["compose"] = kw
        return "// ask script\n"

    monkeypatch.setattr(ask_compose, "compose_ask_script", fake_compose)
    monkeypatch.setattr(op, "check_agent_types_resolve", lambda *a, **k: None)
    monkeypatch.setattr(op, "run_checks", lambda script: [])
    return types.SimpleNamespace(root=tmp_path, calls=calls)


_FIREABLE_S = {
    "schema": "sizing-object",
    "name": "wiring fixture",
    "intent": "exercise the wiring",
    "estimate": {"tshirt": "S", "provisional": True},
    "route": "spec-dispatch",
    "status": "sized",
    "premise": {"provenance": "not-applicable", "evidence": "fixture"},
    "interaction_mode": "pm",
    "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-01", "mode": "pm"}},
}


def _sizing_rel(env, name: str = "a.yaml") -> str:
    (env.root / "state" / "sizings" / name).write_text(
        yaml.safe_dump(_FIREABLE_S), encoding="utf-8"  # C5: emit runs the gate, so the fixture must be fireable
    )
    return f"state/sizings/{name}"


def test_raw_ask_composes_and_writes_to_the_default_run_path(env):
    reply = op._dispatch_emit({"ask": "add a widget"}, repo_root=env.root)
    kw = env.calls["compose"]
    assert kw["prompt"] == "add a widget" and kw["sizing_rel"] is None
    assert kw["run_id"] == reply["run_id"]
    out = Path(reply["path"])
    assert out == env.root / RUN_DIR_ROOT / f"{reply['run_id']}.workflow.mjs"
    assert out.read_text(encoding="utf-8") == "// ask script\n"
    assert reply["receipt"] and Path(reply["receipt"]).is_file()
    assert reply["fire_args"] == {"repoRoot": env.root.as_posix()}
    assert "arm" not in reply


def test_ask_with_sizing_passes_the_sizing_rel_and_no_prompt(env):
    rel = _sizing_rel(env)
    out = env.root / "o.workflow.mjs"
    op._dispatch_emit({"ask": True, "sizing_path": rel, "output_path": str(out)}, repo_root=env.root)
    kw = env.calls["compose"]
    assert kw["sizing_rel"] == rel and kw["prompt"] is None
    assert out.read_text(encoding="utf-8") == "// ask script\n"


def test_bare_sizing_path_is_the_same_entry(env):
    rel = _sizing_rel(env)
    op._dispatch_emit({"sizing_path": rel}, repo_root=env.root)
    assert env.calls["compose"]["sizing_rel"] == rel


def test_ask_is_exclusive_of_other_routes(env):
    for other in ({"plan_path": "p.md"}, {"inventory_path": "i.md"}, {"queue": ["q"]}):
        with pytest.raises(op.SizingPathConflictError):
            op._dispatch_emit({"ask": "x", **other}, repo_root=env.root)


def test_ask_without_any_root_is_refused(env):
    with pytest.raises(ValueError, match="repo_root or target_root"):
        op._dispatch_emit({"ask": "x"})


def test_ask_emit_spawns_nothing(env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned on --ask")

    monkeypatch.setattr(subprocess, "Popen", boom)
    op._dispatch_emit({"ask": "x"}, repo_root=env.root)
    op._dispatch_emit({"sizing_path": _sizing_rel(env)}, repo_root=env.root)
