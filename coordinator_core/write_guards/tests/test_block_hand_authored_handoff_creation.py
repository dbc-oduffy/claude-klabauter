"""Behavioral tests for the hand-authored handoff creation guard and its Bash
twin, driven through each module's ``check()`` entrypoint."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_hand_authored_handoff_creation as bash_guard
from coordinator_core.write_guards import block_hand_authored_handoff_creation as guard

_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_HAND_HANDOFF_WRITE"


@pytest.fixture(autouse=True)
def _clear_override(monkeypatch):
    monkeypatch.delenv(_OVERRIDE_ENV, raising=False)


def _write(tmp_path, rel, *, tool="Write", existing=False):
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if existing:
        target.write_text("---\nkind: session-handoff\n---\n", encoding="utf-8")
    tool_input = {"file_path": str(target)}
    if tool == "Write":
        tool_input["content"] = "hand written"
    else:
        tool_input.update(old_string="a", new_string="b")
    return {"tool_name": tool, "tool_input": tool_input, "cwd": str(tmp_path)}


def _bash(tmp_path, command):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(tmp_path)}


def _denied(result):
    return result is not None and result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_write_in_claude_handoffs_is_denied(tmp_path):
    result = guard.check(_write(tmp_path, ".claude/handoffs/2026-10-06-x.md"))
    assert _denied(result)
    assert "not by hand" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_new_file_in_state_handoffs_is_denied(tmp_path):
    assert _denied(guard.check(_write(tmp_path, "state/handoffs/2026-10-06-x.md")))


def test_edit_to_existing_handoff_passes(tmp_path):
    payload = _write(tmp_path, "state/handoffs/2026-10-06-x.md", tool="Edit", existing=True)
    assert guard.check(payload) is None


def test_existing_file_in_claude_handoffs_passes(tmp_path):
    payload = _write(tmp_path, ".claude/handoffs/2026-10-06-x.md", existing=True)
    assert guard.check(payload) is None


def test_unrelated_path_passes(tmp_path):
    assert guard.check(_write(tmp_path, "docs/plans/p.md")) is None


def test_override_env_bypasses(tmp_path, monkeypatch):
    monkeypatch.setenv(_OVERRIDE_ENV, "1")
    assert guard.check(_write(tmp_path, "state/handoffs/x.md")) is None


@pytest.mark.parametrize(
    "command",
    [
        "baton-assemble --out state/handoffs/2026-10-06-x.md",
        "coordinator-doc-new --type handoff --out state/handoffs/2026-10-06-x.md",
        "coordinator-doc-new --type recovery --recovers-session abc --out state/handoffs/2026-10-06-r.md",
    ],
)
def test_sanctioned_producers_pass_the_bash_arm(tmp_path, command):
    assert bash_guard.check(_bash(tmp_path, command)) is None


def test_recovery_scaffold_then_body_edit_passes(tmp_path):
    # The sanctioned route writes the scaffold with open() inside the engine;
    # the body fill is an Edit of that now-existing path.
    payload = _write(tmp_path, "state/handoffs/2026-10-06-r.md", tool="Edit", existing=True)
    assert guard.check(payload) is None


@pytest.mark.parametrize(
    "command",
    [
        "echo hi > state/handoffs/2026-10-06-x.md",
        "cat body.md >> .claude/handoffs/2026-10-06-x.md",
        "echo hi | tee state/handoffs/2026-10-06-x.md",
        "echo hi | tee -a .claude/handoffs/2026-10-06-x.md",
    ],
)
def test_bash_redirect_and_tee_are_denied(tmp_path, command):
    assert _denied(bash_guard.check(_bash(tmp_path, command)))


def test_bash_redirect_to_existing_handoff_passes(tmp_path):
    target = tmp_path / "state" / "handoffs" / "x.md"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    assert bash_guard.check(_bash(tmp_path, "echo hi >> state/handoffs/x.md")) is None


def test_bash_read_and_fd_dup_pass(tmp_path):
    assert bash_guard.check(_bash(tmp_path, "cat state/handoffs/x.md 2>&1")) is None
    assert bash_guard.check(_bash(tmp_path, "ls state/handoffs")) is None
