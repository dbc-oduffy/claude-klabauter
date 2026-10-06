"""Behavioral tests for block_hand_set_test_verdict."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.completion_receipts.test_verdict import RECORD_VERB
from coordinator_core.write_guards import block_hand_set_test_verdict as guard

_RUNNER = "abcdef0123456789"
_OTHER = "fedcba9876543210"
_REL = "state/subagent-share/sess-1/coordinator-test-runner.%s.md" % _RUNNER


def _edit(old: str, new: str, path: str = _REL, agent_id: str = "") -> dict:
    p = {
        "tool_name": "Edit",
        "tool_input": {"file_path": path, "old_string": old, "new_string": new},
        "session_id": "sess-12345678",
    }
    if agent_id:
        p["agent_id"] = agent_id
    return p


def _denied(r) -> bool:
    return bool(r) and r["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_em_adding_test_verdict_denied():
    assert _denied(guard.check(_edit("status: open", "test_verdict: pass")))


def test_em_status_open_to_pass_denied():
    assert _denied(guard.check(_edit("status: open", "status: pass")))


def test_own_agent_allowed():
    assert guard.check(_edit("status: open", "test_verdict: pass", agent_id=_RUNNER)) is None


def test_different_subagent_denied():
    assert _denied(guard.check(_edit("status: open", "test_verdict: pass", agent_id=_OTHER)))


def test_unchanged_verdict_line_allowed():
    old = "test_verdict: pass\ncommits: []"
    new = "test_verdict: pass\ncommits: [abc]"
    assert guard.check(_edit(old, new)) is None


def test_write_resaving_verdicted_file_allowed(tmp_path: Path):
    f = tmp_path / "state/subagent-share/sess-1" / ("coordinator-test-runner.%s.md" % _RUNNER)
    f.parent.mkdir(parents=True)
    f.write_text("test_verdict: pass\ncommits: []\n", encoding="utf-8")
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(f), "content": "test_verdict: pass\ncommits: [a]\n"},
        "session_id": "sess-12345678",
    }
    assert guard.check(payload) is None
    payload["tool_input"]["content"] = "test_verdict: fail\n"
    assert _denied(guard.check(payload))


def test_non_sidecar_path_allowed():
    assert guard.check(_edit("a", "test_verdict: pass", path="docs/x.md")) is None


def test_backslash_path_handled():
    assert _denied(guard.check(_edit("a", "test_verdict: pass", path=_REL.replace("/", "\\"))))


def test_multiedit_denied():
    p = {
        "tool_name": "MultiEdit",
        "tool_input": {
            "file_path": _REL,
            "edits": [{"old_string": "a", "new_string": "test_verdict: pass"}],
        },
    }
    assert _denied(guard.check(p))


def test_record_verb_pin():
    assert guard._RECORD_VERB == RECORD_VERB
