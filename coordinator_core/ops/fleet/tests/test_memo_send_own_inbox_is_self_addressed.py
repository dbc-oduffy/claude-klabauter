"""memo.send has no own-inbox mode: a memo to the sender's own repo is an ordinary
self-addressed send, and the delivery target derives from the draft's `to:` alone.
No wire param (`to`, `receiver`) can redirect or override it; such a param is refused as unrecognized.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.fleet.memo_send import _memo_send
from coordinator_core.ops.fleet.tests.test_memo_send import (
    _make_claude_home,
    _make_sender_git_repo,
    _write_draft,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _seed_self_send(tmp_path, monkeypatch, topic):
    sender_repo = _make_sender_git_repo(tmp_path)
    inbox = sender_repo / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / ".gitkeep").write_text("", encoding="utf-8")
    claude_home = _make_claude_home(tmp_path, {"project_rag": sender_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    _write_draft(sender_repo, topic, body="Plain body.\n")
    return sender_repo, inbox


def test_self_addressed_draft_delivers_into_own_inbox(tmp_path, monkeypatch):
    sender_repo, inbox = _seed_self_send(tmp_path, monkeypatch, "own-inbox-topic")

    result = _memo_send({"dry_run": False, "topic": "own-inbox-topic"}, repo_root=sender_repo)

    assert result["exit_code"] == 0, result
    assert [p for p in inbox.glob("*.md")], "memo not delivered into the sender's own inbox"


@pytest.mark.parametrize("extra", ["to", "receiver"])
def test_extra_target_param_is_refused_and_writes_nothing(tmp_path, monkeypatch, caplog, extra):
    sender_repo, inbox = _seed_self_send(tmp_path, monkeypatch, "own-inbox-topic")
    draft = sender_repo / "state" / "memo-outbox" / "own-inbox-topic.md"

    result = _memo_send(
        {"dry_run": False, "topic": "own-inbox-topic", extra: "example-retrieval-repo-em"},
        repo_root=sender_repo,
    )

    assert result["exit_code"] != 0, result
    assert "unrecognized param" in caplog.text
    assert not list(inbox.glob("*.md"))
    assert draft.is_file()
