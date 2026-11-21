"""memo.send advises (never holds) when the body claims completion — done /
landed / wired / mapped / handled — without a fenced command-and-output block.

Unit tests exercise `_completion_claims`; the op-level test drives `_memo_send`
and reads `completion_claims` off the acted item.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.fleet.memo_send import _memo_send
from coordinator_core.ops.fleet.tests.test_memo_send import (
    _make_claude_home,
    _make_receiver_git_repo,
    _make_sender_git_repo,
    _write_draft,
)
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture()
def sender_and_receiver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    sender_repo = _make_sender_git_repo(tmp_path)
    receiver_repo = _make_receiver_git_repo(tmp_path)
    claude_home = _make_claude_home(tmp_path, {"project_rag": receiver_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    return sender_repo, receiver_repo


@pytest.mark.cadence
@pytest.mark.spawns_process
class TestOpAdvisory:
    def test_claim_without_block_advises_and_still_delivers(self, sender_and_receiver) -> None:
        sender_repo, receiver_repo = sender_and_receiver
        _write_draft(sender_repo, "claim-topic", body="HRN is handled and wired.\n")
        result = _memo_send({"dry_run": False, "topic": "claim-topic"}, repo_root=sender_repo)
        assert result["exit_code"] == 0, result
        acted = result["acted"][0]
        assert acted["completion_claims"] == ["handled", "wired"]
        assert "command-and-output" in acted["completion_advisory"]
        assert acted["written"] is True

    def test_claim_with_command_and_output_not_advised(self, sender_and_receiver) -> None:
        sender_repo, receiver_repo = sender_and_receiver
        body = "HRN is wired.\n\n```\n$ census\nOK\n```\n"
        _write_draft(sender_repo, "proof-topic", body=body)
        result = _memo_send({"dry_run": False, "topic": "proof-topic"}, repo_root=sender_repo)
        assert result["exit_code"] == 0, result
        assert "completion_claims" not in result["acted"][0]
