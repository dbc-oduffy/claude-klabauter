"""memo.send delivers where `memo_wire.resolve_delivery_target` says, and a `from:`
that sanitizes to an empty slug is a setup error that writes nothing.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.fleet.memo_send import _memo_send
from coordinator_core.ops.fleet.memo_wire import DeliveryTarget, resolve_delivery_target
from coordinator_core.ops.fleet.tests.test_memo_send import (
    _make_claude_home,
    _make_receiver_git_repo,
    _make_sender_git_repo,
    _write_draft,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


@pytest.fixture()
def sender_and_receiver(tmp_path, monkeypatch):
    sender_repo = _make_sender_git_repo(tmp_path)
    receiver_repo = _make_receiver_git_repo(tmp_path)
    claude_home = _make_claude_home(tmp_path, {"project_rag": receiver_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    return sender_repo, receiver_repo


def _inbox_files(receiver_repo):
    return [
        p for p in (receiver_repo / "cross-repo" / "inbox").glob("*.md")
        if p.name != ".gitkeep"
    ]


def test_delivery_path_equals_wire_target(sender_and_receiver):
    sender_repo, receiver_repo = sender_and_receiver
    _write_draft(sender_repo, "wire-topic")

    result = _memo_send({"dry_run": False, "topic": "wire-topic"}, repo_root=sender_repo)
    assert result["exit_code"] == 0, result

    delivered = _inbox_files(receiver_repo)
    assert len(delivered) == 1
    today = delivered[0].name[: len("YYYY-MM-DD")]
    target = resolve_delivery_target(
        "example-retrieval-repo-em", sender_worktree=sender_repo,
        from_id="claude-klabauter-engine", topic="wire-topic", today=today,
    )
    assert isinstance(target, DeliveryTarget)
    assert target.inbox_dir / target.filename == delivered[0]


def test_empty_slug_from_is_a_setup_error_and_writes_nothing(sender_and_receiver, capsys):
    sender_repo, receiver_repo = sender_and_receiver
    draft = _write_draft(sender_repo, "punct-from-topic")
    draft.write_text(
        draft.read_text(encoding="utf-8").replace(
            'from: "claude-klabauter-engine"', 'from: "!!!"'
        ),
        encoding="utf-8", newline="\n",
    )

    result = _memo_send(
        {"dry_run": False, "topic": "punct-from-topic"}, repo_root=sender_repo,
    )

    assert result["exit_code"] == 1, result
    assert "cannot name the delivered file" in capsys.readouterr().err
    assert _inbox_files(receiver_repo) == []
