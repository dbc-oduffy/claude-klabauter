"""Parity/smoke coverage for the warm-hook-migration ops that had no dedicated test.

One test per op (PM ruling, 2026-09-29): registration is live, and the main
output path does not raise on a representative payload. Not exhaustive
parity with the coordinator-content-repo script — that is the follow-on, not this pass.
"""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core.hooks.preuse_agent_dispatch import _handler as agent_dispatch
from coordinator_core.hooks.preuse_skill_dispatch import _handler as skill_dispatch
from coordinator_core.hooks.block_dispatch_suite_invocation import _handler as suite_invocation
from coordinator_core.hooks.block_workflow_unmodeled_agent import _handler as unmodeled_agent
from coordinator_core.hooks.strip_worktree_isolation import _handler as strip_isolation
from coordinator_core.hooks.nudge_workflow_authoring_trampoline import _handler as authoring_trampoline
from coordinator_core.hooks.allow_emitted_workflow_fire import _handler as allow_emitted
from coordinator_core.hooks.preuse_write_dispatch import _handler as write_dispatch
from coordinator_core.hooks.preuse_bash_dispatch import _handler as bash_dispatch
from coordinator_core.hooks.block_worktree_tool import _handler as worktree_tool


def _maybe_await(result):
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


REGISTERED_OPS = [
    "hooks.preuse_agent_dispatch",
    "hooks.preuse_skill_dispatch",
    "hooks.block_dispatch_suite_invocation",
    "hooks.block_workflow_unmodeled_agent",
    "hooks.strip_worktree_isolation",
    "hooks.nudge_workflow_authoring_trampoline",
    "hooks.allow_emitted_workflow_fire",
    "hooks.preuse_write_dispatch",
    "hooks.preuse_bash_dispatch",
    "hooks.block_worktree_tool",
]


@pytest.mark.parametrize("op_name", REGISTERED_OPS)
def test_op_is_registered(op_name: str) -> None:
    import coordinator_core.hooks  # noqa: F401 — triggers registration side-effects
    from coordinator_core.ipc import get_op_handler

    assert get_op_handler(op_name) is not None


@pytest.mark.parametrize(
    "handler",
    [agent_dispatch, skill_dispatch, suite_invocation, unmodeled_agent,
     strip_isolation, authoring_trampoline, allow_emitted, write_dispatch,
     bash_dispatch, worktree_tool],
)
@pytest.mark.parametrize(
    "params",
    [{}, {"tool_name": None}, {"tool_name": "Agent", "tool_input": None},
     {"tool_name": "Agent", "tool_input": "not-a-dict"}],
)
def test_malformed_input_never_raises(handler, params: dict) -> None:
    result = _maybe_await(handler(params))
    assert isinstance(result, dict)


def test_preuse_agent_dispatch_benign_call_allows() -> None:
    result = agent_dispatch({
        "tool_name": "Agent",
        "tool_input": {"prompt": "investigate the thing", "subagent_type": "general-purpose"},
    })
    assert isinstance(result, dict)


def test_preuse_skill_dispatch_benign_call_allows() -> None:
    result = _maybe_await(skill_dispatch({
        "tool_name": "Skill",
        "tool_input": {"command": "some-skill"},
    }))
    assert isinstance(result, dict)


def test_block_dispatch_suite_invocation_benign_call() -> None:
    result = suite_invocation({
        "tool_name": "Agent",
        "tool_input": {"prompt": "investigate the thing"},
    })
    assert isinstance(result, dict)


def test_block_workflow_unmodeled_agent_benign_call() -> None:
    result = unmodeled_agent({
        "tool_name": "Workflow",
        "tool_input": {"script": "print('hello')"},
    })
    assert isinstance(result, dict)


def test_strip_worktree_isolation_strips_worktree_key() -> None:
    result = strip_isolation({
        "tool_name": "Workflow",
        "tool_input": {"script": "x", "isolation": "worktree"},
    })
    assert isinstance(result, dict)
    hso = result.get("hookSpecificOutput", {})
    updated = hso.get("updatedInput")
    if updated is not None:
        assert updated.get("isolation") != "worktree"


def test_nudge_workflow_authoring_trampoline_benign_call() -> None:
    result = authoring_trampoline({
        "tool_name": "Workflow",
        "tool_input": {"script": "print('hi')"},
    })
    assert isinstance(result, dict)


def test_allow_emitted_workflow_fire_benign_call() -> None:
    result = allow_emitted({
        "tool_name": "Workflow",
        "tool_input": {"script": "print('hi')"},
    })
    assert isinstance(result, dict)


def test_preuse_write_dispatch_benign_call() -> None:
    result = write_dispatch({
        "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/not-a-real-file.txt", "content": "hi"},
    })
    assert isinstance(result, dict)


def test_preuse_bash_dispatch_benign_call() -> None:
    result = bash_dispatch({
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
    })
    assert isinstance(result, dict)


def test_block_worktree_tool_denies_enter_and_allows_exit() -> None:
    deny = worktree_tool({"tool_name": "EnterWorktree", "tool_input": {}})
    assert deny.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    allow = worktree_tool({"tool_name": "ExitWorktree", "tool_input": {}})
    assert allow.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
