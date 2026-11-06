"""A Grok-shaped Stop payload carries no `_origin_worktree`; the Stop guard ops
must degrade to a non-error, non-blocking result instead of a routing-key refusal."""

from __future__ import annotations

import asyncio
import json

import pytest

import coordinator_core.ipc as ipc

STOP_GUARD_OPS = (
    "hooks.guard_manufactured_blocker",
    "hooks.guard_turn_intent",
    "hooks.guard_terminal_review",
)


def _grok_stop_payload(workspace, transcript):
    return {
        "hookEventName": "Stop",
        "hook_event_name": "Stop",
        "sessionId": "grok-session-1",
        "session_id": "grok-session-1",
        "cwd": str(workspace),
        "workspaceRoot": str(workspace),
        "transcriptPath": str(transcript),
        "transcript_path": str(transcript),
        "stopHookActive": False,
        "stop_hook_active": False,
    }


@pytest.fixture
def grok_payload(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    return _grok_stop_payload(workspace, transcript)


@pytest.mark.parametrize("op", STOP_GUARD_OPS)
def test_stop_guard_degrades_on_payload_without_origin_worktree(op, grok_payload):
    msg = {"jsonrpc": "2.0", "id": 1, "method": op, "params": {"payload": grok_payload}}
    result = asyncio.run(ipc.dispatch_message(msg))
    assert "error" not in result, f"{op} refused a Grok payload: {result!r}"
    assert '"block"' not in json.dumps(result), f"{op} blocked a Grok payload: {result!r}"


def test_track_touched_files_stays_fail_loud(grok_payload):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "hooks.track_touched_files",
           "params": {"payload": grok_payload}}
    result = asyncio.run(ipc.dispatch_message(msg))
    assert result.get("error", {}).get("code") == ipc.INVALID_PARAMS


def test_stop_guard_ops_are_in_the_degrade_allowlist():
    assert set(STOP_GUARD_OPS) <= ipc._NEVER_DENY_ON_MISSING_KEY_OPS
