"""memo.redeliver lands an existing memo file in its owner's inbox byte-for-byte, or refuses by name."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.fleet import _memo_owning_box as mob
from coordinator_core.ops.fleet import _memo_push_pending, memo_wire
from coordinator_core.ops.fleet._memo_anchor import anchor_names
from coordinator_core.ops.fleet.memo_redeliver import _MODE, _memo_redeliver
from coordinator_core.ops.fleet.tests.test_memo_send import (
    _delivered_text,
    _git,
    _make_claude_home,
    _make_receiver_git_repo,
    _make_sender_git_repo,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_FILENAME = "2026-11-10-claude-klabauter-em-stranded-topic.md"
_SOURCE_SENT_BY = "11111111-2222-3333-4444-555555555555"


def _write_stranded(tmp_path: Path, *, to: str = "example-retrieval-repo-em", body: str = "Stranded body.\n") -> Path:
    """A memo as authored by another EM, sitting in a clone that does not own it."""
    stranded = tmp_path / "stranded-clone" / "state" / "cross-repo" / "inbox"
    stranded.mkdir(parents=True, exist_ok=True)
    path = stranded / _FILENAME
    path.write_text(
        "---\n"
        'title: "A stranded memo"\n'
        'from: "claude-klabauter-em"\n'
        f'to: "{to}"\n'
        "created: 2026-11-10\n"
        'kind: "fyi"\n'
        f'sent_by: "{_SOURCE_SENT_BY}"\n'
        "---\n\n"
        f"{body}",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _repo_state(repo: Path) -> tuple:
    return _git(repo, "for-each-ref").stdout, anchor_names(resolve_git_common_dir(repo))


@pytest.fixture()
def world(tmp_path, monkeypatch):
    sender = _make_sender_git_repo(tmp_path)
    receiver = _make_receiver_git_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"project_rag": receiver})))
    return sender, receiver


class TestPreservedDelivery:
    def test_bytes_and_filename_survive_as_one_anchored_commit(self, tmp_path, world):
        sender, receiver = world
        source = _write_stranded(tmp_path)

        result = _memo_redeliver({"dry_run": False, "path": str(source)}, repo_root=sender)

        assert result["exit_code"] == 0, result
        assert result["mode"] == _MODE
        assert Path(result["acted"][0]["id"]).name == _FILENAME
        delivered = _delivered_text(receiver)
        assert delivered == source.read_text(encoding="utf-8")
        assert 'from: "claude-klabauter-em"' in delivered
        assert f'sent_by: "{_SOURCE_SENT_BY}"' in delivered
        assert "created: 2026-11-10" in delivered
        assert [a[0] for a in anchor_names(resolve_git_common_dir(receiver))] == [_FILENAME]
        assert len(_memo_push_pending.list_pending(sender)) == 1
        assert not (sender / ".coordinator-local" / "memo-outbox" / "sent").exists()

    def test_author_equal_to_the_delivering_clone_is_not_self_addressed(self, tmp_path, world):
        sender, receiver = world
        source = _write_stranded(tmp_path)

        result = _memo_redeliver({"dry_run": False, "path": str(source)}, repo_root=sender)

        assert result["exit_code"] == 0, result
        assert "rejection_class" not in result

    def test_second_call_reconciles_without_a_second_commit(self, tmp_path, world):
        sender, receiver = world
        source = _write_stranded(tmp_path)
        _memo_redeliver({"dry_run": False, "path": str(source)}, repo_root=sender)

        again = _memo_redeliver({"dry_run": False, "path": str(source)}, repo_root=sender)

        assert again["exit_code"] == 0, again
        assert again["acted"][0]["reconciled"] is True
        assert len(_memo_push_pending.list_pending(sender)) == 1
        assert len(anchor_names(resolve_git_common_dir(receiver))) == 1

    def test_dry_run_previews_and_writes_nothing(self, tmp_path, world):
        sender, receiver = world
        source = _write_stranded(tmp_path)
        before = _repo_state(receiver)

        result = _memo_redeliver({"dry_run": True, "path": str(source)}, repo_root=sender)

        assert result["exit_code"] == 0, result
        assert result["candidates"][0]["collision"] is False
        assert _repo_state(receiver) == before
        assert _memo_push_pending.list_pending(sender) == []


class TestRefusals:
    def test_owner_with_no_registered_clone_is_refused_by_class_and_writes_nothing(self, tmp_path, world):
        sender, receiver = world
        source = _write_stranded(tmp_path, to="example-retrieval-repo-blender-addon-em")
        before = _repo_state(receiver)

        result = _memo_redeliver({"dry_run": False, "path": str(source)}, repo_root=sender)

        assert result["exit_code"] == 1, result
        assert result["rejection_class"] == "owner_unreachable"
        assert "UNKNOWN RECEIVER" in result["reason"]
        assert result.get("acted", []) == []
        assert _repo_state(receiver) == before
        assert _memo_push_pending.list_pending(sender) == []

    def test_owner_live_on_another_box_is_refused_by_class(self, tmp_path, monkeypatch, world):
        sender, receiver = world
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        entry = {
            "sessionId": "rag-em-far", "name": "rag-em-far", "pid": os.getpid(), "cwd": str(receiver),
            "unimatrixBox": "otherbox", "unimatrixRemote": "otherbox",
            "unimatrixFarSeen": int(time.time() * 1000),
        }
        (sessions / "rag-em-far.json").write_text(json.dumps(entry), encoding="utf-8")
        monkeypatch.setattr(mob._contract(), "local_box", lambda: "thisbox")
        real = mob.resolve_owning_box
        monkeypatch.setattr(memo_wire, "resolve_owning_box", lambda repo: real(repo, sessions_dir=str(sessions)))
        source = _write_stranded(tmp_path)
        before = _repo_state(receiver)

        result = _memo_redeliver({"dry_run": False, "path": str(source)}, repo_root=sender)

        assert result["exit_code"] == 1, result
        assert result["rejection_class"] == "owner_unreachable"
        assert "NOT THE OWNING BOX" in result["reason"]
        assert _repo_state(receiver) == before

    def test_memo_without_a_sender_is_invalid(self, tmp_path, world):
        sender, receiver = world
        source = _write_stranded(tmp_path)
        source.write_text(source.read_text(encoding="utf-8").replace('from: "claude-klabauter-em"\n', ""), encoding="utf-8")

        result = _memo_redeliver({"dry_run": False, "path": str(source)}, repo_root=sender)

        assert result["exit_code"] == 1, result
        assert result["rejection_class"] == "invalid_memo"

    def test_a_different_memo_under_the_same_filename_is_a_collision(self, tmp_path, world):
        sender, receiver = world
        inbox = receiver / "cross-repo" / "inbox"
        (inbox / _FILENAME).write_text(
            "---\ntitle: \"Other\"\nfrom: \"someone-em\"\nto: \"example-retrieval-repo-em\"\nkind: \"fyi\"\n---\n\nOther body.\n",
            encoding="utf-8", newline="\n",
        )
        _git(receiver, "add", "-A")
        _git(receiver, "commit", "-m", "occupy filename")
        _git(receiver, "push", "-q", "origin", "main")
        source = _write_stranded(tmp_path)
        before = _repo_state(receiver)

        result = _memo_redeliver({"dry_run": False, "path": str(source)}, repo_root=sender)

        assert result["exit_code"] == 2, result
        assert result["failed"][0]["rejection_class"] == "collision"
        assert _repo_state(receiver) == before
        assert _memo_push_pending.list_pending(sender) == []

    def test_unknown_param_is_refused(self, tmp_path, world):
        sender, _ = world
        result = _memo_redeliver({"dry_run": False, "path": "x.md", "to": "y"}, repo_root=sender)
        assert result["exit_code"] == 1
