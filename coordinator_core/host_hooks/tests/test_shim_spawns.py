"""Shim: engine verdict, neutral fallback on down/slow/error, backstop deny, degrade rows, fail-open."""
from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards import block_push_pr_non_owned_remote as push_guard
from coordinator_core.host_hooks import shim
from coordinator_core.warm import telemetry
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

CODEX_LS = {"hook_event_name": "PreToolUse", "session_id": "sess-1", "cwd": "/nonexistent-shim-cwd",
            "tool_name": "exec_command", "tool_input": {"command": "ls"}}
CODEX_PUSH = {**CODEX_LS, "tool_input": {"command": "git push https://github.com/stranger/r.git main"}}
AGY_LS = {"conversationId": "agy-1", "toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}}
AGY_PUSH = {"conversationId": "agy-1", "toolCall": {"name": "run_command", "args": {
    "CommandLine": "git push https://github.com/stranger/r.git main"}}}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(push_guard, "_configured_prefixes", lambda: ["github.com/stranger/"])
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    rows = []
    monkeypatch.setattr(telemetry, "record_degrade", lambda **kw: rows.append(kw))
    return rows


@pytest.fixture
def rows(_env):
    return _env


def _engine(monkeypatch, response, seen=None) -> None:
    def dial(request):
        if seen is not None:
            seen.append(request)
        return response

    monkeypatch.setattr(shim, "_dial", dial)


def _repo_dirs(tmp_path: Path) -> str:
    """A cwd that is a git work tree with an uncommitted file under `work/`."""
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    root = tmp_path / "repo"
    (root / "work").mkdir(parents=True)
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, **no_console_creationflags())
    (root / "work" / "a.py").write_text("# uncommitted\n", encoding="utf-8")
    return str(root)


@pytest.mark.spawns_process
def test_cold_codex_deny_classes_are_denied_and_harmless_commands_allowed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _engine(monkeypatch, None)
    cwd = _repo_dirs(tmp_path)

    def answer(command: str) -> dict:
        return shim.run("codex", json.dumps({**CODEX_LS, "cwd": cwd, "tool_input": {"command": command}}))

    for command in ("rm -rf %s" % (Path(cwd) / "work"), "git stash clear", "git stash drop"):
        assert answer(command)["hookSpecificOutput"]["permissionDecision"] == "deny", command
    assert answer("ls") == {}
    assert answer("git status") == {}


GROK_EDIT = {"cwd": "/nonexistent-shim-cwd", "hook_event_name": "PreToolUse", "tool_name": "edit_file",
             "tool_input": {"path": "/a/b.py", "old_string": "x", "new_string": "y"}}
GROK_SHELL = {**GROK_EDIT, "tool_name": "shell", "tool_input": {"command": "ls"}}
GROK_PUSH = {**GROK_SHELL, "tool_input": {"command": "git push https://github.com/stranger/r.git main"}}


NAG = "[em-code-dispatch nudge] DIY-NAG-SENTINEL"
CODEX_EDIT = {**CODEX_LS, "tool_name": "Edit",
              "tool_input": {"file_path": "/nonexistent-shim-cwd/a.py", "old_string": "x", "new_string": "y"}}
CODEX_WRITE = {**CODEX_EDIT, "tool_name": "Write", "tool_input": {"file_path": "/nonexistent-shim-cwd/a.py", "content": "x"}}


@pytest.mark.spawns_process
def test_evaluation_cold_rm_rf_deny_is_full_chain_backstop_class_with_destructive_rm_deny(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _engine(monkeypatch, None)
    cwd = _repo_dirs(tmp_path)
    shim.take_evaluation()
    out = shim.run("codex", json.dumps({**CODEX_LS, "cwd": cwd,
                                        "tool_input": {"command": "rm -rf %s" % (Path(cwd) / "work")}}))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    evaluation = shim.take_evaluation()
    assert evaluation["route"] == "cold" and evaluation["chain"] == "full" and evaluation["backstop_class"] is True
    assert {"guard": "destructive-rm", "verdict": "deny"} in evaluation["verdicts"]


@pytest.mark.spawns_process
def test_cold_git_clean_row_is_full_chain_backstop_class_with_exact_verdicts(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _engine(monkeypatch, None)
    cwd = _repo_dirs(tmp_path)
    shim.take_evaluation()
    out = shim.run("codex", json.dumps({**CODEX_LS, "cwd": cwd, "tool_input": {"command": "git clean -fdx"}}))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    evaluation = shim.take_evaluation()
    assert evaluation["chain"] == "full" and evaluation["backstop_class"] is True
    assert evaluation["verdicts"] == [{"guard": "backstop", "verdict": "allow"},
                                      {"guard": "destructive-git-clean", "verdict": "deny"}]
