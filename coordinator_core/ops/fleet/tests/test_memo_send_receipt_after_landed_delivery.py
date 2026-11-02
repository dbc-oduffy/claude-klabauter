"""memo.send reconciles a delivery that landed before its sender receipt did.

A send that stalls after the receiver commit leaves the draft in the outbox and the memo in the
receiver's inbox; the next send writes the receipt instead of refusing on the no-clobber guard.
"""

from __future__ import annotations

import shutil

import pytest

from coordinator_core.ops.fleet import memo_send as memo_send_module
from coordinator_core.ops.fleet.memo_send import _memo_send
from coordinator_core.ops.fleet.tests.test_memo_send import (
    _make_claude_home,
    _make_receiver_git_repo,
    _make_sender_git_repo,
    _write_draft,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _stalled_first_send(tmp_path, monkeypatch, topic, **draft_kw):
    sender = _make_sender_git_repo(tmp_path)
    receiver = _make_receiver_git_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"project_rag": receiver})))
    draft = _write_draft(sender, topic, **draft_kw)

    def _stall(*_a, **_k):
        raise ValueError("simulated stall before the sender receipt")

    with monkeypatch.context() as m:
        m.setattr(memo_send_module, "_stamp_sent_copy", _stall)
        first = _memo_send({"dry_run": False, "topic": topic}, repo_root=sender)
    assert first["acted"][0]["delivery_commit_sha"]
    assert draft.exists()
    inbox = receiver / "cross-repo" / "inbox"
    assert len([p for p in inbox.glob("*.md")]) == 1
    return sender, receiver, draft, first["acted"][0]["delivery_commit_sha"]


def test_next_send_writes_receipt_from_anchor_without_redelivering(tmp_path, monkeypatch):
    sender, receiver, draft, sha = _stalled_first_send(tmp_path, monkeypatch, "t-land")
    head_before = (receiver / ".git" / "refs" / "heads" / "main").read_text()

    result = _memo_send({"dry_run": False, "topic": "t-land"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    acted = result["acted"][0]
    assert acted["reconciled"] is True
    assert acted["delivery_commit_sha"] == sha
    assert not draft.exists()
    assert (sender / ".coordinator-local" / "memo-outbox" / "sent" / "t-land.md").is_file()
    assert (receiver / ".git" / "refs" / "heads" / "main").read_text() == head_before


def test_next_send_reconciles_from_tree_copy_when_no_anchor(tmp_path, monkeypatch):
    sender, receiver, draft, _sha = _stalled_first_send(tmp_path, monkeypatch, "t-noanchor")
    shutil.rmtree(receiver / ".git" / "refs" / "coordinator", ignore_errors=True)

    result = _memo_send({"dry_run": False, "topic": "t-noanchor"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    assert result["acted"][0]["reconciled"] is True
    assert result["acted"][0]["delivery_commit_sha"] is None
    assert not draft.exists()


def test_different_memo_under_the_same_filename_still_refuses(tmp_path, monkeypatch):
    sender, receiver, draft, _sha = _stalled_first_send(tmp_path, monkeypatch, "t-other")
    shutil.rmtree(receiver / ".git" / "refs" / "coordinator", ignore_errors=True)
    landed = next((receiver / "cross-repo" / "inbox").glob("*t-other.md"))
    landed.write_text(
        landed.read_text(encoding="utf-8").replace("Body prose.", "A different body."),
        encoding="utf-8",
        newline="\n",
    )

    result = _memo_send({"dry_run": False, "topic": "t-other"}, repo_root=sender)

    assert result["exit_code"] != 0
    assert result["acted"] == []
    assert "collision" in result["failed"][0]["reason"]
    assert draft.exists()


def test_reconcile_registers_the_foreign_delivery_for_push(tmp_path, monkeypatch):
    sender, receiver, _draft, _sha = _stalled_first_send(tmp_path, monkeypatch, "t-push")
    seen = []
    monkeypatch.setattr(
        memo_send_module.push_cadence, "note_foreign_delivery",
        lambda root, branch: seen.append((str(root), branch)),
    )

    result = _memo_send({"dry_run": False, "topic": "t-push"}, repo_root=sender)

    assert result["acted"][0]["reconciled"] is True
    assert [r for r, _b in seen] == [str(receiver)]
    assert seen[0][1].startswith("work/")
