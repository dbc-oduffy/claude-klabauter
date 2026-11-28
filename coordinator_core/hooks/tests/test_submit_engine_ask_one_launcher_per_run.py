"""The hook's resume route starts no second launcher while the run is live, and the stored-script
fire args mint a run token that keeps the running script's token."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.warm import live_runs

pytestmark = pytest.mark.complete

SIZING_REL = "state/sizings/2026-11-21-one-launcher-fixture.yaml"
RUN = "warp-2026-11-21-one-launcher-fixture-20261121T010101-abc123"
FIRST_LAUNCHER = "state/warp/first-launcher.workflow.mjs"


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "XS", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "interaction_mode": "pm", "deliverable_id": "dlv-one-launcher-abc123",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-11-21", "mode": "pm"}},
    }
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    rd = tmp_path / RECORD_DIR_ROOT / RUN
    rd.mkdir(parents=True)
    (rd / "halted.json").write_text(json.dumps({"status": "halted", "halted": "commit", "sizing_rel": SIZING_REL}), encoding="utf-8")
    (rd / "run-stage.json").write_text(json.dumps({"stage": "commit"}), encoding="utf-8")
    return tmp_path


def _route(root: Path, monkeypatch: pytest.MonkeyPatch, *, live: bool):
    monkeypatch.setattr(hrr, "engine_sha", lambda: None)
    monkeypatch.setattr(live_runs, "run_is_live", lambda r, run_id, **k: live)
    spawned: list = []
    monkeypatch.setattr(mod, "_spawn_recompose", lambda r, run_id: spawned.append(run_id) or True)
    payload = {
        "tool_name": "Workflow", "cwd": str(root), "session_id": "s1", "tool_use_id": "toolu_x",
        "tool_input": {"name": mod.SUBMIT_WORKFLOW_NAME, "args": {"sizing": SIZING_REL, "resume_run_id": RUN}},
    }
    params = {"session_id": "s1", "ask": True, "sizing_path": SIZING_REL}
    envelope, result = mod._resume_route(root, SIZING_REL, payload, params, "s1", None)
    return envelope, result, spawned


def _log(root: Path) -> Path:
    return root / RECORD_DIR_ROOT / RUN / hrr.REFIRE_LOG_FILE


def _text(envelope: dict) -> str:
    return json.dumps(envelope)


def test_a_live_run_with_a_refire_line_is_refused_naming_its_launcher(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    hrr.write_refire_record(root / RECORD_DIR_ROOT / RUN, FIRST_LAUNCHER, "launcher", None, 1.0)
    before = _log(root).read_text(encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("a live-run refusal spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    envelope, result, spawned = _route(root, monkeypatch, live=True)

    assert result is None and envelope is not None
    assert RUN in _text(envelope) and FIRST_LAUNCHER in _text(envelope)
    assert _log(root).read_text(encoding="utf-8") == before
    assert spawned == []


def test_a_live_run_with_no_refire_line_is_refused_naming_the_run_alone(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    envelope, result, spawned = _route(root, monkeypatch, live=True)

    assert result is None and envelope is not None
    assert RUN in _text(envelope) and "by launcher" not in _text(envelope)
    assert not _log(root).exists()
    assert spawned == []


def test_a_run_that_is_not_live_composes_its_launcher(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    envelope, result, _spawned = _route(root, monkeypatch, live=False)

    assert envelope is not None and "scriptPath" in _text(envelope)
    assert hrr.latest_launcher(root / RECORD_DIR_ROOT / RUN)["kind"] == "launcher"


def test_the_stored_script_fire_args_mint_with_carry_prior(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_mint(root, run_id, script_path, **kwargs):
        seen.update(kwargs)
        return "tok"

    monkeypatch.setattr(run_token, "mint", fake_mint)
    out = mod._resume_fire_args(tmp_path, RUN, SIZING_REL, "s1")

    assert seen.get("carry_prior") is True
    assert out == {"fire_args": {"repoRoot": tmp_path.as_posix(), "runToken": "tok"}}
