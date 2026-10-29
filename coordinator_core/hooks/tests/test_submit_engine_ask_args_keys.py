"""Unknown args / asks-entry keys are refused by name; an entry's context and a top-level sender reach the engine."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.hooks import submit_engine_ask as mod
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
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ctx.md").write_text("x", encoding="utf-8")


def _reason(env: dict) -> str:
    return env["hookSpecificOutput"]["permissionDecisionReason"]


def _boom(*_a, **_k):
    raise AssertionError("the engine must not be reached")


def test_unknown_top_level_key_denies_naming_it_and_the_accepted_keys(tmp_path):
    _repo(tmp_path)
    env = mod.evaluate(_payload(tmp_path, {"ask": "x", "contxt": ["a"]}), emit=_boom, fanout=_boom)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = _reason(env)
    assert "args: unknown key 'contxt'" in reason
    assert all(key in reason for key in mod._ARGS_KEYS)


def test_args_keys_cover_every_key_the_hook_reads():
    assert {"ask", "asks", "sizing", "context", "em_context", "brief", "brief_file", "sender"} <= set(mod._ARGS_KEYS)
    assert len(set(mod._ARGS_KEYS)) == len(mod._ARGS_KEYS)


def test_unknown_entry_key_denies_before_the_fanout(tmp_path):
    _repo(tmp_path)
    args = {"asks": [{"ask": "one"}, {"ask": "two", "contxt": ["a"]}]}
    env = mod.evaluate(_payload(tmp_path, args), emit=_boom, fanout=_boom)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = _reason(env)
    assert "asks[1]: unknown key 'contxt'" in reason
    assert all(key in reason for key in ASKS_ITEM_KEYS)


def test_after_is_an_accepted_entry_key_and_reaches_the_fanout(tmp_path):
    _repo(tmp_path)
    assert "after" in ASKS_ITEM_KEYS
    seen: list[dict] = []

    def fanout(params, root):
        seen.append(params)
        return {"error": "stop here"}

    args = {"asks": [{"ask": "one", "label": "a"}, {"ask": "two", "after": ["a"]}]}
    mod.evaluate(_payload(tmp_path, args), emit=_boom, fanout=fanout)
    assert seen[0]["asks"][1]["after"] == ["a"]


def test_after_naming_no_earlier_sibling_denies_naming_the_key(tmp_path):
    _repo(tmp_path)
    args = {"asks": [{"ask": "one", "after": ["two"]}, {"ask": "two", "label": "two"}]}
    env = mod.evaluate(_payload(tmp_path, args), emit=_boom, fanout=_boom)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "asks[0].after" in _reason(env)


def test_single_element_asks_context_reaches_the_emit(tmp_path):
    _repo(tmp_path)
    seen: list[dict] = []

    def emit(params: dict, root: Path) -> dict:
        seen.append(params)
        return {"path": str(tmp_path / "run.js"), "run_id": "r1", "fire_args": {}}

    args = {"asks": [{"ask": "fix it", "context": ["docs/ctx.md", "a concrete host bug"]}]}
    env = mod.evaluate(_payload(tmp_path, args), emit=emit)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert seen[0]["ask_context"] == ["docs/ctx.md"]
    assert "a concrete host bug" in seen[0]["ask_brief"]
    assert seen[0]["ask"] == "fix it"


def test_single_element_asks_carries_top_level_context_first(tmp_path):
    _repo(tmp_path)
    (tmp_path / "docs" / "two.md").write_text("y", encoding="utf-8")
    seen: list[dict] = []

    def emit(params: dict, root: Path) -> dict:
        seen.append(params)
        return {"path": str(tmp_path / "run.js"), "run_id": "r1", "fire_args": {}}

    args = {"context": "docs/ctx.md", "asks": [{"ask": "fix it", "context": "docs/two.md"}]}
    mod.evaluate(_payload(tmp_path, args), emit=emit)
    assert seen[0]["ask_context"] == ["docs/ctx.md", "docs/two.md"]


def test_entry_context_must_be_string_or_list_of_strings(tmp_path):
    _repo(tmp_path)
    args = {"asks": [{"ask": "a"}, {"ask": "b", "context": [1]}]}
    env = mod.evaluate(_payload(tmp_path, args), emit=_boom, fanout=_boom)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "asks[1].context must be a string" in _reason(env)


def test_missing_entry_context_file_denies_naming_the_entry(tmp_path):
    _repo(tmp_path)
    args = {"asks": [{"ask": "a"}, {"ask": "b", "context": ["docs/nope.md"]}]}
    env = mod.evaluate(_payload(tmp_path, args), emit=_boom, fanout=_boom)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "asks[1].context docs/nope.md: no such file" in _reason(env)


def test_top_level_context_beside_multi_asks_denies(tmp_path):
    _repo(tmp_path)
    args = {"context": "docs/ctx.md", "asks": [{"ask": "a"}, {"ask": "b"}]}
    env = mod.evaluate(_payload(tmp_path, args), emit=_boom, fanout=_boom)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "context rides each asks item (asks[i].context), not the call." in _reason(env)


def test_top_level_sender_reaches_the_fanout(tmp_path):
    _repo(tmp_path)
    seen: list[dict] = []

    def fanout(params: dict, root: Path) -> dict:
        seen.append(params)
        return {"error": "stop here"}

    args = {"sender": "example-retrieval-repo-d1", "asks": [{"ask": "a"}, {"ask": "b", "context": ["why"]}]}
    mod.evaluate(_payload(tmp_path, args), fanout=fanout)
    assert seen[0]["sender"] == "example-retrieval-repo-d1"
    assert seen[0]["asks"][1]["context"] == ["why"]


def test_sizing_with_valid_source_is_not_refused_for_the_source(tmp_path):
    _repo(tmp_path)
    seen: list[dict] = []

    def emit(params: dict, root: Path) -> dict:
        seen.append(params)
        return {"path": str(tmp_path / "run.js"), "run_id": "r1", "fire_args": {}}

    env = mod.evaluate(_payload(tmp_path, {"sizing": "state/sizings/x.yaml", "source": "relayed"}), emit=emit, fanout=_boom)
    assert "source marks an ask" not in env["hookSpecificOutput"].get("permissionDecisionReason", "")
    assert seen and all("ask_source" not in p for p in seen)


def test_sizing_with_invalid_source_still_refuses_naming_it(tmp_path):
    _repo(tmp_path)
    env = mod.evaluate(_payload(tmp_path, {"sizing": "state/sizings/x.yaml", "source": "bogus"}), emit=_boom, fanout=_boom)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "source 'bogus' is not one of" in _reason(env)


def test_hook_keeps_no_local_copy_of_the_moved_helpers():
    assert not hasattr(mod, "_with_em_context") and not hasattr(mod, "_is_path_shaped")


def test_top_level_sender_beside_a_one_element_asks_still_reaches_the_fanout(tmp_path):
    _repo(tmp_path)
    seen: list[dict] = []

    def fanout(params: dict, root: Path) -> dict:
        seen.append(params)
        return {"error": "stop here"}

    mod.evaluate(_payload(tmp_path, {"sender": "example-retrieval-repo-d1", "asks": [{"ask": "a"}]}), emit=_boom, fanout=fanout)
    assert seen[0]["sender"] == "example-retrieval-repo-d1"
