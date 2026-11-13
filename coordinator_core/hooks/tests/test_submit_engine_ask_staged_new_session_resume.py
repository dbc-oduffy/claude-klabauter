"""A refire from a new session resumes a run left at run-stage `staged` whose owner session is gone,
keeping its wave-0 record; a staged run whose owner is live is never adopted."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import live_run_index
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_STAGE_FILE

SIZING_REL = "state/sizings/2026-11-05-staged-fixture.yaml"
RUN_ID = "warp-2026-11-05-staged-fixture-20261105T010101-abc123"
DEAD = "dead-session"
LIVE = "live-session"
NEW = "new-session"
WAVE0 = "wave-0.out.json"


def _repo(tmp_path: Path, owner: str) -> Path:
    (tmp_path / ".git").mkdir()
    sizing = tmp_path / SIZING_REL
    sizing.parent.mkdir(parents=True, exist_ok=True)
    sizing.write_text("schema: sizing-object\n", encoding="utf-8")
    rd = tmp_path / RECORD_DIR_ROOT / RUN_ID
    rd.mkdir(parents=True)
    (rd / "run.json").write_text(json.dumps({"sizing_rel": SIZING_REL}), encoding="utf-8")
    stage = {"stage": "staged", "session_id": owner, "started_at": "2026-11-05T01:01:01Z"}
    (rd / RUN_STAGE_FILE).write_text(json.dumps(stage), encoding="utf-8")
    (rd / WAVE0).write_text('{"wave": 0}', encoding="utf-8")
    # The refire finds an unmarked run only through the live-run index, never a `state/warp` listing.
    assert live_run_index.add(tmp_path, RUN_ID, 0.0)
    return tmp_path


@pytest.fixture
def refire(monkeypatch):
    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda sid: sid == LIVE)
    spawned: list = []
    monkeypatch.setattr(mod, "_spawn_recompose", lambda root, run_id: spawned.append(run_id) or True)

    def run(root: Path):
        emitted: list = []
        launched: list = []
        script = root / "launcher.js"
        script.write_text("//", encoding="utf-8")

        def emit(params, r):
            emitted.append(params)
            return {"path": str(r / "run.js"), "run_id": "fresh", "fire_args": {}}

        def launch(r, parent_id, children, session_id):
            launched.append([c.run_id for c in children])
            return script

        payload = {
            "tool_name": "Workflow", "cwd": str(root), "session_id": NEW, "tool_use_id": "toolu_x",
            "tool_input": {"name": mod.SUBMIT_WORKFLOW_NAME, "args": {"sizing": SIZING_REL}},
        }
        return mod.evaluate(payload, emit=emit, launch=launch), emitted, launched

    return run


def _stage(root: Path) -> dict:
    return json.loads((root / RECORD_DIR_ROOT / RUN_ID / RUN_STAGE_FILE).read_text(encoding="utf-8"))


def test_dead_owner_staged_run_is_resumed_under_its_run_id(tmp_path, refire):
    root = _repo(tmp_path, DEAD)
    env, emitted, launched = refire(root)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    assert emitted == [] and launched == [[RUN_ID]]


def test_adoption_keeps_wave_zero_and_hands_the_run_to_the_refiring_session(tmp_path, refire):
    root = _repo(tmp_path, DEAD)
    refire(root)
    rd = root / RECORD_DIR_ROOT / RUN_ID
    assert (rd / WAVE0).read_text(encoding="utf-8") == '{"wave": 0}'
    marker = json.loads((rd / "halted.json").read_text(encoding="utf-8"))
    assert marker["status"] == "halted"
    stage = _stage(root)
    assert stage["session_id"] == NEW and stage["adopted_from"] == [DEAD] and stage["stage"] == "halted"


def test_live_owner_staged_run_is_not_adopted(tmp_path, refire):
    root = _repo(tmp_path, LIVE)
    env, emitted, launched = refire(root)
    rd = root / RECORD_DIR_ROOT / RUN_ID
    assert not (rd / "halted.json").exists()
    assert _stage(root)["session_id"] == LIVE
    assert "adopted_from" not in _stage(root)
    assert emitted == []
