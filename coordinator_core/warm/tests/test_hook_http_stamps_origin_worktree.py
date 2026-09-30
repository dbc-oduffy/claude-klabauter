"""An HTTP-delivered hook event must reach a common_dir-scoped op with its routing key.

Without `_origin_worktree` every such op answers -32602 before its handler runs.
"""
import asyncio
import json
from pathlib import Path

import pytest

from coordinator_core import ipc
from coordinator_core.warm import hook_http

_REPO = str(Path(__file__).resolve().parents[3])

_EVENTS = [
    ("hooks.subagent_zero_tool_use_detect",
     {"hook_event_name": "SubagentStop", "session_id": "t", "cwd": _REPO, "agent_id": "x"}),
    ("hooks.postuse_agent_dispatch",
     {"hook_event_name": "PostToolUse", "session_id": "t", "cwd": _REPO, "tool_name": "Agent",
      "tool_input": {"description": "d"}, "tool_response": {"agentId": "a"}}),
    ("hooks.preuse_agent_dispatch",
     {"hook_event_name": "PreToolUse", "session_id": "t", "cwd": _REPO, "tool_name": "Agent",
      "tool_input": {"description": "d", "prompt": "p"}}),
]


@pytest.mark.parametrize("op,event", _EVENTS, ids=[e[0] for e in _EVENTS])
def test_envelope_carries_origin_worktree_and_is_not_invalid_params(op, event):
    req = json.loads(hook_http.build_request(event, op))
    assert req["_origin_worktree"] == _REPO
    req.pop("_caller", None)
    resp = asyncio.run(ipc.dispatch_message(req))
    assert resp.get("error", {}).get("code") != ipc.INVALID_PARAMS, resp


def test_no_cwd_stamps_nothing():
    req = json.loads(hook_http.build_request({"hook_event_name": "Stop"}, "hooks.x"))
    assert "_origin_worktree" not in req
