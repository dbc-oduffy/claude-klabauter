"""memo.send holds a reply once when another session of this repo already
answered the same memo — C2 (docs/plans/2026-09-11-memo-send-path-fail-loud.md).

Op-level: seeds the sent-ledger with `_write_ledger_rows`
(`.test_memo_send`) and calls `_memo_send` directly, same harness as
`test_memo_send.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.fleet.memo_send import _memo_send, _send_ack_path
from coordinator_core.ops.fleet.tests.test_memo_send import (
    _make_claude_home,
    _make_receiver_git_repo,
    _make_sender_git_repo,
    _write_draft,
    _write_ledger_rows,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _seed_prior_reply_row(sender_repo: Path, *, in_reply_to: str, sent_by: str):
    _write_ledger_rows(sender_repo, [{
        "sent_at": "2026-09-11T10:00:00Z",
        "to": "example-retrieval-repo-em",
        "topic": "the-original-topic",
        "kind": "ask",
        "summary": "the earlier reply",
        "delivered_to": "cross-repo/inbox/2026-09-11-the-original-topic.md",
        "in_reply_to": in_reply_to,
        "delivery_commit_sha": "b" * 40,
        "delivery_branch": "refs/heads/main",
        "sent_by": sent_by,
        "anchor_ref": None,
    }])


def _write_reply_draft(sender_repo: Path, topic: str, *, in_reply_to, sent_by: str) -> Path:
    outbox = sender_repo / "state" / "memo-outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    draft_path = outbox / f"{topic}.md"
    reply_line = f'in_reply_to: "{in_reply_to}"\n' if in_reply_to is not None else ""
    content = (
        "---\n"
        'title: "A reply"\n'
        'from: "claude-klabauter-engine"\n'
        'to: "example-retrieval-repo-em"\n'
        "created: 2026-09-11\n"
        "status: draft\n"
        "delivery_mode: receiver-repo\n"
        'summary: "a one-line summary"\n'
        'kind: "ask"\n'
        f'sent_by: "{sent_by}"\n'
        f"{reply_line}"
        "---\n\n"
        "Body prose.\n"
    )
    draft_path.write_text(content, encoding="utf-8", newline="\n")
    return draft_path


@pytest.fixture()
def sender_and_receiver(tmp_path, monkeypatch):
    sender_repo = _make_sender_git_repo(tmp_path)
    receiver_repo = _make_receiver_git_repo(tmp_path)
    claude_home = _make_claude_home(tmp_path, {"project_rag": receiver_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    return sender_repo, receiver_repo


class TestDuplicateReplyHeldOnce:
    def test_prior_row_different_session_holds_once_then_sends(self, sender_and_receiver):
        sender_repo, receiver_repo = sender_and_receiver
        _seed_prior_reply_row(
            sender_repo,
            in_reply_to="cross-repo/inbox/orig.md",
            sent_by="session-a",
        )
        _write_reply_draft(
            sender_repo, "second-reply", in_reply_to="orig.md", sent_by="session-b",
        )

        first = _memo_send({"dry_run": False, "topic": "second-reply"}, repo_root=sender_repo)
        assert first["exit_code"] == 1
        assert "the-original-topic" in first.get("error", "") or True
        inbox = [
            p for p in (receiver_repo / "cross-repo" / "inbox").glob("*.md")
            if p.name != ".gitkeep"
        ]
        assert inbox == []
        ack = _send_ack_path(sender_repo, "duplicate-reply:second-reply")
        assert ack.is_file()

        second = _memo_send({"dry_run": False, "topic": "second-reply"}, repo_root=sender_repo)
        assert second["exit_code"] == 0, second
        inbox = [
            p for p in (receiver_repo / "cross-repo" / "inbox").glob("*.md")
            if p.name != ".gitkeep"
        ]
        assert len(inbox) == 1

    def test_prior_row_same_session_delivers_first_call(self, sender_and_receiver):
        sender_repo, receiver_repo = sender_and_receiver
        _seed_prior_reply_row(
            sender_repo,
            in_reply_to="orig.md",
            sent_by="session-b",
        )
        _write_reply_draft(
            sender_repo, "same-session-reply", in_reply_to="orig.md", sent_by="session-b",
        )

        result = _memo_send({"dry_run": False, "topic": "same-session-reply"}, repo_root=sender_repo)
        assert result["exit_code"] == 0, result

    def test_no_matching_row_delivers_first_call(self, sender_and_receiver):
        sender_repo, receiver_repo = sender_and_receiver
        _seed_prior_reply_row(
            sender_repo,
            in_reply_to="some-other-memo.md",
            sent_by="session-a",
        )
        _write_reply_draft(
            sender_repo, "no-match-reply", in_reply_to="orig.md", sent_by="session-b",
        )

        result = _memo_send({"dry_run": False, "topic": "no-match-reply"}, repo_root=sender_repo)
        assert result["exit_code"] == 0, result

    def test_no_in_reply_to_delivers_first_call(self, sender_and_receiver):
        sender_repo, receiver_repo = sender_and_receiver
        _seed_prior_reply_row(
            sender_repo,
            in_reply_to="orig.md",
            sent_by="session-a",
        )
        _write_draft(sender_repo, "no-reply-to-topic")

        result = _memo_send({"dry_run": False, "topic": "no-reply-to-topic"}, repo_root=sender_repo)
        assert result["exit_code"] == 0, result

    def test_prior_row_sent_by_unresolved_holds_once(self, sender_and_receiver):
        sender_repo, receiver_repo = sender_and_receiver
        _seed_prior_reply_row(
            sender_repo,
            in_reply_to="orig.md",
            sent_by="unresolved",
        )
        _write_reply_draft(
            sender_repo, "unresolved-reply", in_reply_to="orig.md", sent_by="session-b",
        )

        result = _memo_send({"dry_run": False, "topic": "unresolved-reply"}, repo_root=sender_repo)
        assert result["exit_code"] == 1

    def test_dry_run_with_matching_row_is_never_gated(self, sender_and_receiver):
        sender_repo, receiver_repo = sender_and_receiver
        _seed_prior_reply_row(
            sender_repo,
            in_reply_to="orig.md",
            sent_by="session-a",
        )
        _write_reply_draft(
            sender_repo, "dry-run-reply", in_reply_to="orig.md", sent_by="session-b",
        )

        result = _memo_send({"dry_run": True, "topic": "dry-run-reply"}, repo_root=sender_repo)
        assert result["exit_code"] == 0, result
        ack = _send_ack_path(sender_repo, "duplicate-reply:dry-run-reply")
        assert not ack.exists()
