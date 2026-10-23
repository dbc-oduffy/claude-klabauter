"""engine-ask `chatty` arg: a boolean reaches the emit op as params["chatty"]; absent forwards nothing."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.hooks import submit_engine_ask as mod


def _payload(tmp_path: Path, args: dict) -> dict:
    return {
        "tool_name": "Workflow",
        "cwd": str(tmp_path),
        "session_id": "s1",
        "tool_input": {"name": mod.SUBMIT_WORKFLOW_NAME, "args": args},
    }


def _run(tmp_path: Path, args: dict):
    (tmp_path / ".git").mkdir(exist_ok=True)
    seen: list[dict] = []

    def emit(params: dict, root: Path) -> dict:
        seen.append(params)
        return {"path": str(tmp_path / "run.js"), "run_id": "r1", "fire_args": {}}

    env = mod.evaluate(_payload(tmp_path, args), emit=emit)
    return env, seen


def _decision(env: dict) -> str:
    return env["hookSpecificOutput"]["permissionDecision"]


def test_top_level_chatty_true_reaches_emit(tmp_path):
    env, seen = _run(tmp_path, {"ask": "do it", "chatty": True})
    assert _decision(env) == "allow"
    assert seen[0]["chatty"] is True


def test_absent_chatty_forwards_no_key(tmp_path):
    env, seen = _run(tmp_path, {"ask": "do it"})
    assert _decision(env) == "allow"
    assert "chatty" not in seen[0]


def test_chatty_false_forwards_no_key(tmp_path):
    _, seen = _run(tmp_path, {"ask": "do it", "chatty": False})
    assert "chatty" not in seen[0]


def test_non_boolean_chatty_is_refused(tmp_path):
    env, seen = _run(tmp_path, {"ask": "do it", "chatty": "yes"})
    assert _decision(env) == "deny"
    assert "chatty must be true or false" in env["hookSpecificOutput"]["permissionDecisionReason"]
    assert seen == []


def test_single_element_asks_item_chatty_reaches_emit(tmp_path):
    env, seen = _run(tmp_path, {"asks": [{"ask": "do it", "chatty": True}]})
    assert _decision(env) == "allow"
    assert seen[0]["chatty"] is True


def test_non_boolean_item_chatty_is_refused(tmp_path):
    env, seen = _run(tmp_path, {"asks": [{"ask": "do it", "chatty": 1}]})
    assert _decision(env) == "deny"
    assert "asks[0].chatty must be true or false" in env["hookSpecificOutput"]["permissionDecisionReason"]
    assert seen == []


def test_multi_item_entry_chatty_is_refused(tmp_path):
    env, seen = _run(tmp_path, {"asks": [{"ask": "a"}, {"ask": "b", "chatty": True}]})
    assert _decision(env) == "deny"
    assert "chatty is not carried by a multi-item asks" in env["hookSpecificOutput"]["permissionDecisionReason"]
    assert seen == []


def test_top_level_chatty_with_multi_item_asks_is_refused(tmp_path):
    env, seen = _run(tmp_path, {"asks": [{"ask": "a"}, {"ask": "b"}], "chatty": True})
    assert _decision(env) == "deny"
    assert "chatty is not carried by a multi-item asks" in env["hookSpecificOutput"]["permissionDecisionReason"]
    assert seen == []
