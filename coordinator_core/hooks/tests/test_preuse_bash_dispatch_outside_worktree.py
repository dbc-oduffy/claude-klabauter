"""A Bash call whose cwd is outside every git worktree still gets its guards evaluated.

The whole door is under test (`dispatch_message`, not the bare handler): the routing-key
step in front of the handler is what once answered such a call with a "Missing required
routing key" refusal, so a handler-only test cannot see that class.
"""

from __future__ import annotations

import asyncio

from coordinator_core.ipc import dispatch_message


def _dispatch_bash(cwd, command) -> dict:
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "hooks.preuse_bash_dispatch",
        "params": {
            "payload": {
                "tool_name": "Bash",
                "tool_input": {"command": command},
                "cwd": str(cwd),
                "session_id": "s",
            }
        },
    }
    return asyncio.run(dispatch_message(msg))


def test_banned_command_is_denied_when_cwd_is_outside_a_worktree(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    result = _dispatch_bash(tmp_path, f"git worktree add {tmp_path / 'x'}")
    assert "error" not in result, result
    hso = result["result"]["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"


def test_no_unevaluated_advisory_when_cwd_is_outside_a_worktree(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    result = _dispatch_bash(tmp_path, "echo hi")
    assert "error" not in result, result
    context = (result["result"].get("hookSpecificOutput") or {}).get("additionalContext") or ""
    assert "could not be evaluated" not in context
