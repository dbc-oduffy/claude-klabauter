"""Every refire shape composes against the halted run's base; a fresh fire composes against HEAD."""

from __future__ import annotations

import json

import pytest
import yaml

import coordinator_core.git.git_state as git_state
from coordinator_core.ops.dispatch_emit import ask_compose, op
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.halted_run import HALTED_FILE, write_halt_marker

REL = "state/sizings/2026-10-16-refire-c3b.yaml"
WARP = "warp-2026-10-16-refire-c3b-20260101T000000-aaaaaa"
ASK = "ask-20261016T120000-bbbbbb"
BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
HEAD = "c" * 40
BASE = "a" * 40


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: BLITZ)
    monkeypatch.setattr(git_state, "head_sha", lambda *_a, **_k: HEAD)
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "XS", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
        "interaction_mode": "pm", "deliverable_id": "dlv-refire-abc123",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-16", "mode": "pm"}},
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return tmp_path


@pytest.fixture
def composed(monkeypatch):
    seen = []
    real = ask_compose.compose_ask_script

    def spy(**kw):
        seen.append(kw.get("run_base_sha"))
        return real(**kw)

    monkeypatch.setattr(ask_compose, "compose_ask_script", spy)
    return seen


def _halt(repo, run_id, base):
    rl.start(repo, run_id)
    rl.bind(repo, run_id, REL, ask_text="the ask")
    write_halt_marker(record_dir(repo, run_id), halted="criterion", sizing_rel=REL, run_base_sha=base)


def _status(repo, run_id):
    return json.loads((record_dir(repo, run_id) / HALTED_FILE).read_text(encoding="utf-8"))["status"]


def _fire(repo):
    return op._dispatch_emit({"ask": True, "sizing_path": REL}, repo_root=repo)


def test_a_rebound_refire_with_the_scratch_base_gone_composes_against_the_marker_base(repo, composed):
    _halt(repo, WARP, BASE)
    assert op._recorded_run_base(repo, WARP) is None
    assert _fire(repo)["run_id"] == WARP
    assert composed == [BASE]
    assert op._recorded_run_base(repo, WARP) == BASE


def test_a_halted_ask_run_predecessor_yields_a_fresh_warp_run_on_its_base(repo, composed):
    _halt(repo, ASK, BASE)
    reply = _fire(repo)
    assert reply["run_id"] != ASK and reply["run_id"].startswith("warp-2026-10-16-refire-c3b-")
    assert composed == [BASE]
    assert op._recorded_run_base(repo, reply["run_id"]) == BASE
    assert _status(repo, ASK) == "resumed"


def test_a_halted_ask_run_without_a_base_falls_back_to_head(repo, composed):
    _halt(repo, ASK, None)
    reply = _fire(repo)
    assert composed == [None]
    assert op._recorded_run_base(repo, reply["run_id"]) == HEAD


def test_a_fresh_fire_composes_against_head(repo, composed):
    reply = _fire(repo)
    assert composed == [None]
    assert op._recorded_run_base(repo, reply["run_id"]) == HEAD
