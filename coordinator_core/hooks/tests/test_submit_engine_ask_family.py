"""A `family` arg on {ask} and asks entries is validated at submit and forwarded to the emit op; an unknown
name and one beside {sizing} are refused naming the family."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import stage_family
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import ASKS_ITEM_KEYS


def _payload(tmp_path: Path, args: dict) -> dict:
    return {
        "tool_name": "Workflow",
        "cwd": str(tmp_path),
        "session_id": "s1",
        "tool_input": {"name": mod.SUBMIT_WORKFLOW_NAME, "args": args},
    }


def _repo(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()


def _reason(env: dict) -> str:
    return env["hookSpecificOutput"]["permissionDecisionReason"]


def _decision(env: dict) -> str:
    return env["hookSpecificOutput"]["permissionDecision"]


def _boom(*_a, **_k):
    raise AssertionError("the engine must not be reached")


def _emit_capture(tmp_path: Path, seen: list[dict]):
    def emit(params: dict, root: Path) -> dict:
        seen.append(params)
        return {"path": str(tmp_path / "run.js"), "run_id": "r1", "fire_args": {}}

    return emit


def test_family_is_an_accepted_arg_and_entry_key_before_writes():
    assert "family" in mod._ARGS_KEYS
    assert "family" in ASKS_ITEM_KEYS and ASKS_ITEM_KEYS[-3:] == ("family", "writes", "stop_after")


@pytest.mark.parametrize("family", ["codex", "agy", "grok"])
def test_ask_family_reaches_the_emit(tmp_path, family):
    _repo(tmp_path)
    seen: list[dict] = []
    env = mod.evaluate(_payload(tmp_path, {"ask": "x", "family": family}), emit=_emit_capture(tmp_path, seen))
    assert _decision(env) == "allow"
    assert seen[0]["family"] == family


def test_claude_family_and_absent_family_add_no_param(tmp_path):
    _repo(tmp_path)
    seen: list[dict] = []
    emit = _emit_capture(tmp_path, seen)
    assert _decision(mod.evaluate(_payload(tmp_path, {"ask": "x", "family": "claude"}), emit=emit)) == "allow"
    mod.evaluate(_payload(tmp_path, {"ask": "x"}), emit=emit)
    assert len(seen) == 2 and all("family" not in p for p in seen)


def test_one_element_asks_family_reaches_the_emit(tmp_path):
    _repo(tmp_path)
    seen: list[dict] = []
    env = mod.evaluate(
        _payload(tmp_path, {"asks": [{"ask": "x", "family": "codex"}]}), emit=_emit_capture(tmp_path, seen)
    )
    assert _decision(env) == "allow"
    assert seen[0]["family"] == "codex"


def test_top_level_family_beside_one_element_asks_reaches_the_emit(tmp_path):
    _repo(tmp_path)
    seen: list[dict] = []
    mod.evaluate(_payload(tmp_path, {"family": "grok", "asks": [{"ask": "x"}]}), emit=_emit_capture(tmp_path, seen))
    assert seen[0]["family"] == "grok"


def test_multi_item_asks_family_rides_each_fanout_item(tmp_path):
    _repo(tmp_path)
    seen: list[dict] = []

    def fanout(params: dict, root: Path) -> dict:
        seen.append(params)
        return {"error": "stop here"}

    args = {"family": "agy", "asks": [{"ask": "a", "family": "codex"}, {"ask": "b"}]}
    mod.evaluate(_payload(tmp_path, args), emit=_boom, fanout=fanout)
    assert [item.get("family") for item in seen[0]["asks"]] == ["codex", "agy"]


@pytest.mark.parametrize("bad", ["gpt", "", "Codex", 3, ["codex"]])
def test_unknown_ask_family_denies_naming_it_and_the_accepted_set(tmp_path, bad):
    _repo(tmp_path)
    env = mod.evaluate(_payload(tmp_path, {"ask": "x", "family": bad}), emit=_boom, fanout=_boom)
    assert _decision(env) == "deny"
    assert repr(bad) in _reason(env)
    assert "|".join(stage_family.FAMILIES) in _reason(env)


def test_unknown_entry_family_denies_naming_the_entry_and_the_family(tmp_path):
    _repo(tmp_path)
    args = {"asks": [{"ask": "a"}, {"ask": "b", "family": "gpt"}]}
    env = mod.evaluate(_payload(tmp_path, args), emit=_boom, fanout=_boom)
    assert _decision(env) == "deny"
    assert "asks[1].family 'gpt'" in _reason(env)


def test_family_beside_sizing_denies_naming_it_and_the_sizing_key(tmp_path):
    _repo(tmp_path)
    env = mod.evaluate(
        _payload(tmp_path, {"sizing": "state/sizings/x.yaml", "family": "agy"}), emit=_boom, fanout=_boom
    )
    assert _decision(env) == "deny"
    assert "'agy'" in _reason(env) and "sizing's own `family` key governs" in _reason(env)


def test_sizing_without_family_still_reaches_the_emit_with_none(tmp_path):
    _repo(tmp_path)
    seen: list[dict] = []
    mod.evaluate(
        _payload(tmp_path, {"sizing": "state/sizings/x.yaml"}), emit=_emit_capture(tmp_path, seen), fanout=_boom
    )
    assert seen and all("family" not in p for p in seen)
