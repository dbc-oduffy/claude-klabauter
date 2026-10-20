"""An engine-ask submission carrying route_preference and em_rationale, through the hook's `evaluate` with
the real `dispatch.emit` on a tmp repo: the run record holds both and the size-stage scout prompt renders them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core import warp_fire
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.route_preference import ROUTE_PREFERENCE_FILE
from coordinator_core.session_baton import store

SID = "sess-route-pref-e2e"
VERBATIM = "make the exporter streaming"
RATIONALE = "spans two workstreams and needs a sequenced roadmap"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "DELIVERABLE_ID"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    (root / "state" / "sizings").mkdir(parents=True)
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    assert PT.append_turn(VERBATIM, session_id=SID)["ok"]
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return root


def _submit(root: Path, args: dict) -> dict:
    return mod.evaluate({
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": args},
        "session_id": SID,
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    })


def _hso(env: dict) -> dict:
    return env["hookSpecificOutput"]


def _run_id(env: dict) -> str:
    return Path(_hso(env)["updatedInput"]["scriptPath"]).name.removesuffix(".workflow.mjs")


def test_a_preference_and_rationale_persist_in_the_run_record_and_reach_the_scout_prompt(repo):
    env = _submit(repo, {"ask": "make the exporter streaming", "source": "pm", "route_preference": "roadmap",
                         "em_rationale": RATIONALE})

    assert _hso(env)["permissionDecision"] == "allow", env
    record = record_dir(repo, _run_id(env)) / ROUTE_PREFERENCE_FILE
    assert json.loads(record.read_text(encoding="utf-8")) == {
        "route_preference": "roadmap", "em_rationale": RATIONALE,
    }
    script = Path(_hso(env)["updatedInput"]["scriptPath"]).read_text(encoding="utf-8")
    assert "## EM route preference" in script
    assert "roadmap" in script and RATIONALE in script


def test_a_submission_without_a_preference_writes_no_record_and_no_block(repo):
    env = _submit(repo, {"ask": "make the exporter streaming", "source": "pm"})

    assert _hso(env)["permissionDecision"] == "allow", env
    assert not (record_dir(repo, _run_id(env)) / ROUTE_PREFERENCE_FILE).exists()
    assert "## EM route preference" not in Path(_hso(env)["updatedInput"]["scriptPath"]).read_text(encoding="utf-8")


@pytest.mark.parametrize("args", [
    {"ask": "x", "route_preference": "shape"},
    {"ask": "x", "route_preference": "plan", "em_rationale": "y" * 2001},
], ids=["bad-enum", "over-cap"])
def test_an_invalid_preference_is_denied_naming_the_key_and_writes_nothing(repo, args):
    env = _submit(repo, args)

    assert _hso(env)["permissionDecision"] == "deny"
    assert ("route_preference" if args["route_preference"] == "shape" else "em_rationale") in (
        _hso(env)["permissionDecisionReason"]
    )
    assert not (repo / "scratch" / "warp").exists()
