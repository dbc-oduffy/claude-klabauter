"""Behavioral tests for the same-repo peer-contention notice channel:
coordinator_core.ops.peer_notice_send / peer_notice_check.

Covers:
  1. send() writes a notice file under state/peer-notices/<target>/ with the
     expected fields.
  2. send() requires target_session_id / artifact_path / message; missing any
     raises ValueError.
  3. check()/list_unread_notices() returns only unread (non-.delivered) notices,
     oldest-created_at-first, for the addressed session.
  4. check() with no notices for the session returns an empty list, no raise.
  5. A notice moved into .delivered/ (simulating guard delivery) is excluded
     from the unread listing.
  6. An unsafe target_session_id / session_id (path traversal shape) raises
     ValueError rather than being resolved.
  7. list_unread_notices sorts by created_at, not by the random uuid4
     filename.
  8. An oversized message is truncated at send time.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops import peer_notice_check, peer_notice_send


def _repo_root(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_send_writes_notice_file(tmp_path, monkeypatch):
    root = _repo_root(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.ops.peer_notice_send.main_worktree_root", lambda p: root
    )
    monkeypatch.setattr(
        "coordinator_core.ops.peer_notice_send.harness_registry.snapshot", lambda: {}
    )

    result = peer_notice_send._peer_notice_send(
        {
            "target_session_id": "peer-abc",
            "artifact_path": "coordinator_core/foo.py",
            "message": "I am editing this function",
            "enclosing_function": "do_thing",
            "from_session_id": "self-xyz",
        },
        repo_root=root,
    )

    assert result["ok"] is True
    notice_path = root / result["notice_path"]
    assert notice_path.is_file()
    record = json.loads(notice_path.read_text(encoding="utf-8"))
    assert record["target_session_id"] == "peer-abc"
    assert record["from_session_id"] == "self-xyz"
    assert record["artifact_path"] == "coordinator_core/foo.py"
    assert record["enclosing_function"] == "do_thing"
    assert record["message"] == "I am editing this function"
    assert "created_at" in record


@pytest.mark.parametrize(
    "params",
    [
        {"artifact_path": "x.py", "message": "hi"},
        {"target_session_id": "peer-abc", "message": "hi"},
        {"target_session_id": "peer-abc", "artifact_path": "x.py"},
    ],
)
def test_send_requires_fields(tmp_path, monkeypatch, params):
    root = _repo_root(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.ops.peer_notice_send.main_worktree_root", lambda p: root
    )
    with pytest.raises(ValueError):
        peer_notice_send._peer_notice_send(params, repo_root=root)


def test_check_lists_unread_only(tmp_path, monkeypatch):
    root = _repo_root(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.ops.peer_notice_send.main_worktree_root", lambda p: root
    )
    monkeypatch.setattr(
        "coordinator_core.ops.peer_notice_send.harness_registry.snapshot", lambda: {}
    )
    monkeypatch.setattr(
        "coordinator_core.ops.peer_notice_check.main_worktree_root", lambda p: root
    )

    peer_notice_send._peer_notice_send(
        {
            "target_session_id": "peer-abc",
            "artifact_path": "a.py",
            "message": "first",
            "from_session_id": "sender-1",
        },
        repo_root=root,
    )
    peer_notice_send._peer_notice_send(
        {
            "target_session_id": "peer-abc",
            "artifact_path": "b.py",
            "message": "second",
            "from_session_id": "sender-2",
        },
        repo_root=root,
    )

    result = peer_notice_check._peer_notice_check(
        {"session_id": "peer-abc"}, repo_root=root
    )
    assert len(result["notices"]) == 2
    assert {n["message"] for n in result["notices"]} == {"first", "second"}
    for n in result["notices"]:
        assert "_path" not in n


def test_check_empty_for_unknown_session(tmp_path, monkeypatch):
    root = _repo_root(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.ops.peer_notice_check.main_worktree_root", lambda p: root
    )
    result = peer_notice_check._peer_notice_check(
        {"session_id": "nobody-ever-sent-here"}, repo_root=root
    )
    assert result == {"notices": []}


def test_delivered_notice_excluded_from_unread(tmp_path, monkeypatch):
    root = _repo_root(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.ops.peer_notice_send.main_worktree_root", lambda p: root
    )
    monkeypatch.setattr(
        "coordinator_core.ops.peer_notice_send.harness_registry.snapshot", lambda: {}
    )
    monkeypatch.setattr(
        "coordinator_core.ops.peer_notice_check.main_worktree_root", lambda p: root
    )

    send_result = peer_notice_send._peer_notice_send(
        {
            "target_session_id": "peer-abc",
            "artifact_path": "a.py",
            "message": "first",
            "from_session_id": "sender-1",
        },
        repo_root=root,
    )
    notice_path = root / send_result["notice_path"]
    delivered_dir = notice_path.parent / ".delivered"
    delivered_dir.mkdir()
    notice_path.replace(delivered_dir / notice_path.name)

    result = peer_notice_check._peer_notice_check(
        {"session_id": "peer-abc"}, repo_root=root
    )
    assert result["notices"] == []


@pytest.mark.parametrize("bad_id", ["../escape", "a/b", "a\\b", "..", "."])
def test_send_rejects_unsafe_target_session_id(tmp_path, monkeypatch, bad_id):
    root = _repo_root(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.ops.peer_notice_send.main_worktree_root", lambda p: root
    )
    with pytest.raises(ValueError):
        peer_notice_send._peer_notice_send(
            {
                "target_session_id": bad_id,
                "artifact_path": "x.py",
                "message": "hi",
            },
            repo_root=root,
        )


@pytest.mark.parametrize("bad_id", ["../escape", "a/b", "a\\b", "..", "."])
def test_check_rejects_unsafe_session_id(tmp_path, monkeypatch, bad_id):
    root = _repo_root(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.ops.peer_notice_check.main_worktree_root", lambda p: root
    )
    with pytest.raises(ValueError):
        peer_notice_check._peer_notice_check({"session_id": bad_id}, repo_root=root)


def test_list_unread_notices_orders_by_created_at_not_filename(tmp_path, monkeypatch):
    root = _repo_root(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.ops.peer_notice_send.main_worktree_root", lambda p: root
    )
    monkeypatch.setattr(
        "coordinator_core.ops.peer_notice_send.harness_registry.snapshot", lambda: {}
    )

    notices_dir = root / "state" / "peer-notices" / "peer-abc"
    notices_dir.mkdir(parents=True)
    # Filenames deliberately in REVERSE chronological order relative to
    # created_at, so a filename-sort would return them wrong-way-round.
    (notices_dir / "aaa-first-by-name.json").write_text(
        json.dumps({"created_at": "2026-08-15T12:00:00+00:00", "message": "newest"}),
        encoding="utf-8",
    )
    (notices_dir / "zzz-last-by-name.json").write_text(
        json.dumps({"created_at": "2026-08-15T10:00:00+00:00", "message": "oldest"}),
        encoding="utf-8",
    )

    notices = peer_notice_check.list_unread_notices(root, "peer-abc")
    assert [n["message"] for n in notices] == ["oldest", "newest"]


def test_send_truncates_oversized_message(tmp_path, monkeypatch):
    root = _repo_root(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.ops.peer_notice_send.main_worktree_root", lambda p: root
    )
    monkeypatch.setattr(
        "coordinator_core.ops.peer_notice_send.harness_registry.snapshot", lambda: {}
    )

    oversized = "x" * (peer_notice_send._MAX_MESSAGE_LEN + 500)
    result = peer_notice_send._peer_notice_send(
        {
            "target_session_id": "peer-abc",
            "artifact_path": "a.py",
            "message": oversized,
        },
        repo_root=root,
    )
    record = json.loads((root / result["notice_path"]).read_text(encoding="utf-8"))
    assert len(record["message"]) < len(oversized)
    assert record["message"].endswith("...[truncated]")
