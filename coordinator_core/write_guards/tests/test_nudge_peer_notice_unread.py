"""Behavioral tests for coordinator_core.write_guards.nudge_peer_notice_unread
-- the peer-contention notice delivery seam.

Covers:
  1. No notices for the session -> silent (None).
  2. One unread notice -> fires, additionalContext names sender + artifact,
     never a permissionDecision.
  3. Firing MOVES the notice into .delivered/ -- a second call is silent.
  4. Bounded surfacing: more than _MAX_NOTICES unread notices -> only
     _MAX_NOTICES rendered, "...and N more" suffix present.
  5. Missing session_id / non-mutating tool -> silent, no exception.
  6. Module contract: CLASS/MATCHERS/PRIORITY shape.
"""

from __future__ import annotations

import json

from coordinator_core.ops import peer_notice_send
from coordinator_core.write_guards import nudge_peer_notice_unread as guard


def _repo_root(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _send(root, monkeypatch, target, message, sender="sender-1", artifact="a.py"):
    monkeypatch.setattr(
        "coordinator_core.ops.peer_notice_send.main_worktree_root", lambda p: root
    )
    monkeypatch.setattr(
        "coordinator_core.ops.peer_notice_send.harness_registry.snapshot", lambda: {}
    )
    return peer_notice_send._peer_notice_send(
        {
            "target_session_id": target,
            "artifact_path": artifact,
            "message": message,
            "from_session_id": sender,
        },
        repo_root=root,
    )


def _payload(tool_name, session_id, cwd):
    return {
        "tool_name": tool_name,
        "tool_input": {"file_path": "whatever.py"},
        "session_id": session_id,
        "cwd": str(cwd),
    }


def test_no_notices_is_silent(tmp_path):
    root = _repo_root(tmp_path)
    result = guard.check(_payload("Write", "peer-abc", root))
    assert result is None


def test_one_notice_fires_and_never_denies(tmp_path, monkeypatch):
    root = _repo_root(tmp_path)
    _send(root, monkeypatch, "peer-abc", "I am editing this function")

    result = guard.check(_payload("Edit", "peer-abc", root))
    assert result is not None
    envelope = result["hookSpecificOutput"]
    assert envelope["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in envelope
    assert "sender-1" in envelope["additionalContext"]
    assert "a.py" in envelope["additionalContext"]
    assert "PEER NOTICE" in envelope["additionalContext"]


def test_firing_delivers_notice_so_second_call_is_silent(tmp_path, monkeypatch):
    root = _repo_root(tmp_path)
    _send(root, monkeypatch, "peer-abc", "collision")

    first = guard.check(_payload("Write", "peer-abc", root))
    assert first is not None

    second = guard.check(_payload("Write", "peer-abc", root))
    assert second is None

    delivered_dir = root / "state" / "peer-notices" / "peer-abc" / ".delivered"
    assert delivered_dir.is_dir()
    assert len(list(delivered_dir.glob("*.json"))) == 1


def test_bounded_surfacing(tmp_path, monkeypatch):
    root = _repo_root(tmp_path)
    for i in range(guard._MAX_NOTICES + 2):
        _send(root, monkeypatch, "peer-abc", f"msg-{i}", artifact=f"f{i}.py")

    result = guard.check(_payload("Write", "peer-abc", root))
    assert result is not None
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "...and 2 more" in context


def test_missing_session_id_is_silent(tmp_path):
    root = _repo_root(tmp_path)
    payload = _payload("Write", "", root)
    assert guard.check(payload) is None


def test_non_mutating_tool_is_silent(tmp_path, monkeypatch):
    root = _repo_root(tmp_path)
    _send(root, monkeypatch, "peer-abc", "hi")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "session_id": "peer-abc",
        "cwd": str(root),
    }
    assert guard.check(payload) is None


def test_module_contract():
    assert guard.CLASS == "advisory"
    assert set(guard.MATCHERS) == {"Write", "Edit", "MultiEdit", "NotebookEdit"}
    assert isinstance(guard.PRIORITY, int)


def test_unsafe_session_id_is_silent_not_raised(tmp_path):
    root = _repo_root(tmp_path)
    payload = _payload("Write", "../escape", root)
    # An advisory guard must never raise into the caller's write -- an
    # unusable id degrades to "nothing to surface," same as no notices.
    assert guard.check(payload) is None
