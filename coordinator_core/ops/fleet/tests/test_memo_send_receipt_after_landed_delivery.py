"""memo.send reconciles a delivery that landed before its sender receipt did.

A send that stalls after the receiver delivery leaves the draft in the outbox, the delivery commit
anchored in the receiver and its push owed in the sender's push-pending outbox; the next send writes
the receipt instead of delivering again or refusing on the no-clobber guard.
"""

from __future__ import annotations

import shutil

import pytest

from coordinator_core.ops.fleet import memo_send as memo_send_module
from coordinator_core.ops.fleet.memo_send import _memo_send
from coordinator_core.ops.fleet.tests.test_memo_send import (
    _delivered_text,
    _git,
    _make_claude_home,
    _make_receiver_git_repo,
    _make_sender_git_repo,
    _pending_records,
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
    assert len(_pending_records(sender)) == 1
    return sender, receiver, draft, first["acted"][0]["delivery_commit_sha"]


def _publish_copy_in_receiver(receiver, sha, text=None):
    """Land the delivered memo in the receiver's own pushed history and checkout, as a
    pull of the pushed delivery would."""
    names = _git(receiver, "ls-tree", "--name-only", f"{sha}:cross-repo/inbox").stdout.decode()
    name = next(n for n in names.split() if n.endswith(".md"))
    body = text if text is not None else _delivered_text(receiver)
    (receiver / "cross-repo" / "inbox" / name).write_text(body, encoding="utf-8", newline="\n")
    _git(receiver, "add", "-A")
    _git(receiver, "commit", "-q", "-m", "memo pulled in")
    _git(receiver, "push", "-q", "origin", "main")
    return receiver / "cross-repo" / "inbox" / name


def test_next_send_writes_receipt_from_anchor_without_redelivering(tmp_path, monkeypatch):
    sender, receiver, draft, sha = _stalled_first_send(tmp_path, monkeypatch, "t-land")
    head_before = (receiver / ".git" / "refs" / "heads" / "main").read_text()

    result = _memo_send({"dry_run": False, "topic": "t-land"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    acted = result["acted"][0]
    assert acted["reconciled"] is True
    assert acted["delivery_commit_sha"] == sha
    assert "push_pending_record" not in acted
    assert not draft.exists()
    assert (sender / ".coordinator-local" / "memo-outbox" / "sent" / "t-land.md").is_file()
    assert (receiver / ".git" / "refs" / "heads" / "main").read_text() == head_before


def test_reconcile_keeps_the_one_owed_push_record(tmp_path, monkeypatch):
    sender, _receiver, _draft, sha = _stalled_first_send(tmp_path, monkeypatch, "t-push")

    result = _memo_send({"dry_run": False, "topic": "t-push"}, repo_root=sender)

    assert result["acted"][0]["reconciled"] is True
    assert [r["commit_sha"] for r in _pending_records(sender)] == [sha]


def test_a_landed_memo_with_no_owed_push_is_not_reconciled_from_its_anchor(tmp_path, monkeypatch):
    """An anchor alone is a finished delivery: with no push-pending record naming it, the next send
    of the same topic delivers again rather than skipping the receiver."""
    sender, receiver, draft, sha = _stalled_first_send(tmp_path, monkeypatch, "t-done")
    for row in _pending_records(sender):
        from coordinator_core.ops.fleet import _memo_push_pending

        _memo_push_pending.remove_pending(sender, row["commit_sha"])

    result = _memo_send({"dry_run": False, "topic": "t-done"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    acted = result["acted"][0]
    assert "reconciled" not in acted
    assert acted["push_pending_record"]
    assert len(_pending_records(sender)) == 1


def test_next_send_reconciles_from_tree_copy_when_no_anchor(tmp_path, monkeypatch):
    sender, receiver, draft, sha = _stalled_first_send(tmp_path, monkeypatch, "t-noanchor")
    _publish_copy_in_receiver(receiver, sha)
    shutil.rmtree(receiver / ".git" / "refs" / "coordinator", ignore_errors=True)

    result = _memo_send({"dry_run": False, "topic": "t-noanchor"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    assert result["acted"][0]["reconciled"] is True
    assert result["acted"][0]["delivery_commit_sha"] is None
    assert not draft.exists()


def test_different_memo_under_the_same_filename_still_refuses(tmp_path, monkeypatch):
    sender, receiver, draft, sha = _stalled_first_send(tmp_path, monkeypatch, "t-other")
    different = _delivered_text(receiver).replace("Body prose.", "A different body.")
    _publish_copy_in_receiver(receiver, sha, text=different)
    shutil.rmtree(receiver / ".git" / "refs" / "coordinator", ignore_errors=True)

    result = _memo_send({"dry_run": False, "topic": "t-other"}, repo_root=sender)

    assert result["exit_code"] != 0
    assert result["acted"] == []
    assert "collision" in result["failed"][0]["reason"]
    assert draft.exists()
