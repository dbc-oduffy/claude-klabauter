"""block_subagent_commit refuses a subagent identified by any of agent_id,
agent_type, or a `subagents/` transcript_path, and never the EM."""

from __future__ import annotations

from coordinator_core.bash_guards import block_subagent_commit as guard

_CMD = 'git commit -m "x"'
_SUB_TP = "/h/projects/p/sess/subagents/agent-a1.jsonl"


def _payload(**extra):
    p = {
        "tool_name": "Bash",
        "tool_input": {"command": _CMD},
        "session_id": "sess1",
        "cwd": None,
    }
    p.update(extra)
    return p


def _denied(result):
    return (
        result is not None
        and result["hookSpecificOutput"]["permissionDecision"] == "deny"
    )


def test_agent_id_is_refused():
    assert _denied(guard.check(_payload(agent_id="deadbeef0123")))


def test_subagent_transcript_path_without_agent_id_is_refused():
    assert _denied(guard.check(_payload(transcript_path=_SUB_TP)))


def test_windows_subagent_transcript_path_is_refused():
    tp = "C:\\h\\projects\\p\\sess\\subagents\\agent-a1.jsonl"  # abs-path-ok: fixture string, never touched
    assert _denied(guard.check(_payload(transcript_path=tp)))


def test_unlisted_agent_type_without_agent_id_is_refused():
    assert _denied(guard.check(_payload(agent_type="coordinator:executor")))


def test_em_payload_is_allowed():
    assert guard.check(_payload(transcript_path="/h/projects/p/sess.jsonl")) is None
    assert guard.check(_payload()) is None
