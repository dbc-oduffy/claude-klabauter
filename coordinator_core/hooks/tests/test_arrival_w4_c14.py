
from __future__ import annotations

import asyncio

import pytest


def _run(coro):
    return asyncio.run(coro)


def test_guard_kira_verdict_routed_registers():
    from coordinator_core.ipc import _REGISTRY
    import coordinator_core.hooks.guard_kira_verdict_routed  # noqa: F401

    assert "hooks.guard_kira_verdict_routed" in _REGISTRY


def test_guard_kira_verdict_routed_skips_on_subagent_stop():
    from coordinator_core.hooks.guard_kira_verdict_routed import (
        _guard_kira_verdict_routed_handler,
    )

    out = _guard_kira_verdict_routed_handler({"payload": {"agent_id": "sub-1"}})
    assert out == {}


def test_guard_kira_verdict_routed_no_session_id_is_advisory_not_block():
    from coordinator_core.hooks.guard_kira_verdict_routed import (
        _guard_kira_verdict_routed_handler,
    )

    out = _guard_kira_verdict_routed_handler({"payload": {}})
    hso = out.get("hookSpecificOutput")
    assert hso is not None
    assert hso.get("permissionDecision") != "deny"


def test_guard_manufactured_blocker_registers():
    from coordinator_core.ipc import _REGISTRY
    import coordinator_core.hooks.guard_manufactured_blocker  # noqa: F401

    assert "hooks.guard_manufactured_blocker" in _REGISTRY


def test_guard_manufactured_blocker_no_op_on_subagent_stop():
    from coordinator_core.hooks.guard_manufactured_blocker import _handler

    assert _handler({"payload": {"agent_id": "sub-1"}}) == {}


def test_guard_manufactured_blocker_no_op_without_transcript_path():
    from coordinator_core.hooks.guard_manufactured_blocker import _handler

    assert _handler({"payload": {}}) == {}


def test_guard_manufactured_blocker_fires_on_handoff_construct(tmp_path):
    from coordinator_core.hooks.guard_manufactured_blocker import _handler

    transcript = tmp_path / "transcript.jsonl"
    entry = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Three things now wait on you: A, B, C."}
            ]
        },
    }
    import json

    transcript.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    out = _handler(
        {
            "payload": {
                "transcript_path": str(transcript),
                "session_id": "sess-1",
                "cwd": str(tmp_path),
            }
        }
    )
    hso = out.get("hookSpecificOutput")
    assert hso is not None
    assert hso.get("additionalContext") or hso.get("permissionDecisionReason")


def test_postuse_stop_family_dispatch_registers():
    from coordinator_core.ipc import _REGISTRY
    import coordinator_core.hooks.postuse_stop_family_dispatch  # noqa: F401

    assert "hooks.postuse_stop_family_dispatch" in _REGISTRY


def test_postuse_stop_family_dispatch_no_op_on_non_dict_payload():
    from coordinator_core.hooks.postuse_stop_family_dispatch import _handler

    assert _run(_handler({"payload": None})) == {}


def test_sessionend_auto_commit_registers():
    from coordinator_core.ipc import _REGISTRY
    import coordinator_core.hooks.sessionend_auto_commit  # noqa: F401

    assert "hooks.sessionend_auto_commit" in _REGISTRY


def test_sessionend_auto_commit_no_op_without_session_id():
    from coordinator_core.hooks.sessionend_auto_commit import _handler

    assert _handler({"payload": {}}) == {}


def test_subagent_zero_tool_use_detect_registers():
    from coordinator_core.ipc import _REGISTRY
    import coordinator_core.hooks.subagent_zero_tool_use_detect  # noqa: F401

    assert "hooks.subagent_zero_tool_use_detect" in _REGISTRY


def test_subagent_zero_tool_use_detect_no_op_without_agent_type():
    from coordinator_core.hooks.subagent_zero_tool_use_detect import _handler

    assert _run(_handler({"payload": {}})) == {}


def test_subagent_zero_tool_use_detect_no_op_when_not_dispatched_this_session(
    tmp_path,
):
    from coordinator_core.hooks.subagent_zero_tool_use_detect import _handler

    (tmp_path / ".git").mkdir()
    out = _run(
        _handler(
            {
                "payload": {
                    "agent_type": "coordinator:executor",
                    "session_id": "sess-1",
                    "agent_id": "agent-1",
                    "cwd": str(tmp_path),
                }
            }
        )
    )
    assert out == {}


def test_group_em_park_spool_registers():
    from coordinator_core.ipc import _REGISTRY
    import coordinator_core.hooks.group_em_park_spool  # noqa: F401

    assert "hooks.group_em_park_spool" in _REGISTRY


def test_group_em_park_spool_no_op_without_session_id():
    from coordinator_core.hooks.group_em_park_spool import _handler

    assert _handler({"payload": {}}) == {}


def test_group_em_park_spool_no_op_without_state_dir(tmp_path):
    from coordinator_core.hooks.group_em_park_spool import _handler

    (tmp_path / ".git").mkdir()
    out = _handler(
        {"payload": {"session_id": "sess-1", "cwd": str(tmp_path)}}
    )
    assert out == {}


def test_group_em_park_spool_build_record_only_spools_paused():
    from coordinator_core.hooks.group_em_park_spool import build_record

    assert build_record("sess-1", {"verdict": "ACTIVE"}) is None
    assert build_record("sess-1", {"verdict": "PAUSED"}) is None
    record = build_record(
        "sess-1", {"verdict": "PAUSED", "stamped_at": "2026-09-18T00:00:00Z"}
    )
    assert record == {
        "session_id": "sess-1",
        "state": "PAUSED",
        "at": "2026-09-18T00:00:00Z",
        "writer": "receiver-state-sensor",
    }


def test_stop_dispatch_reexports_guard_kira_verdict_routed():
    import coordinator_core.hooks.stop_dispatch as stop_dispatch
    import coordinator_core.hooks.guard_kira_verdict_routed as extracted

    assert (
        stop_dispatch._guard_kira_verdict_routed_handler
        is extracted._guard_kira_verdict_routed_handler
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_day_branch_assert_loads_the_engines_own_session_ensure_branch():
    from coordinator_core.hooks import day_branch_assert

    day_branch_assert._session_ensure_branch = None
    fn = day_branch_assert._load_session_ensure_branch()
    assert callable(fn)
    assert day_branch_assert._load_session_ensure_branch() is fn
