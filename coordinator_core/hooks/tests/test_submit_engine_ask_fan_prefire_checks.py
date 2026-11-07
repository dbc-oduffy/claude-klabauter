"""The multi-ask fan's pre-fire checks: B1 and B2 refuse in the hook through the default `evaluate(payload)`;
the per-child emit checks (L1-L9) run in the launcher child, so the hook never emits a fan child."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import warp_fire, warp_live
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.session_baton import store

SID = "sid-fan-prefire"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "DELIVERABLE_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    root = tmp_path / "repo"
    (root / "state" / "sizings").mkdir(parents=True)
    (root / ".git").mkdir()
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    assert PT.append_turn("pm turn with some words", session_id=SID)["ok"]
    return root


def _payload(root, second_writes="d1"):
    asks = [
        {"ask": "job 0", "source": "pm", "label": "j0", "writes": ["d0"]},
        {"ask": "job 1", "source": "pm", "label": "j1", "writes": [second_writes]},
    ]
    return {
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": {"asks": asks}},
        "session_id": SID,
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    }


def _reason(env):
    return env["hookSpecificOutput"]["permissionDecisionReason"]


def _no_launcher(root):
    return not list((root / "scratch").glob("**/*.launcher.workflow.mjs"))


def test_a_clean_fan_is_allowed_and_the_hook_emits_no_child(repo, monkeypatch):
    def no_emit(*a, **k):
        raise AssertionError("the hook emitted a fan child")

    monkeypatch.setattr(mod, "_default_emit", no_emit)
    env = mod.evaluate(_payload(repo))
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env


def test_b1_a_live_run_on_a_ready_sizing_refuses_the_fan_before_the_launcher(repo, monkeypatch):
    calls = []

    def live(root, sizing_rel, **kw):
        calls.append(sizing_rel)
        return "ask-live-run" if len(calls) == 2 else None

    monkeypatch.setattr(warp_live, "live_run_for_sizing", live)
    env = mod.evaluate(_payload(repo))
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny", env
    assert "ask-live-run is already working" in _reason(env)
    assert _no_launcher(repo)


def test_b2_a_write_outside_every_checkout_refuses_the_fan_before_the_launcher(repo):
    env = mod.evaluate(_payload(repo, "../no-such-sibling-xyz/file.py"))
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny", env
    assert "outside every git checkout" in _reason(env)
    assert _no_launcher(repo)


def test_b2_an_absolute_write_in_a_distant_checkout_asks_for_approval_not_unreachable(repo, tmp_path):
    distant = tmp_path / "deep" / "elsewhere" / "ue-project"
    (distant / ".git").mkdir(parents=True)
    env = mod.evaluate(_payload(repo, str(distant / "Config" / ".gitignore")))
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny", env
    assert "approve_cross_repo_write" in _reason(env)
    assert "outside every git checkout" not in _reason(env)
    assert _no_launcher(repo)


def test_a_refused_child_emit_is_named_by_the_launcher_after_the_hook_answered(repo):
    env = mod.evaluate(_payload(repo))
    text = Path(env["hookSpecificOutput"]["updatedInput"]["scriptPath"]).read_text(encoding="utf-8")
    assert "status: 'emit_refused'" in text
