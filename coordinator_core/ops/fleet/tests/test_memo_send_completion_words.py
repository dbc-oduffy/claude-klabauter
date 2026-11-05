"""memo.send advises (never holds) when the body claims completion — done /
landed / wired / mapped / handled — without a fenced command-and-output block.

Unit tests exercise `_completion_claims`; the op-level test drives `_memo_send`
and reads `completion_claims` off the acted item.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.fleet.memo_send import _completion_claims, _memo_send
from coordinator_core.ops.fleet.tests.test_memo_send import (
    _make_claude_home,
    _make_receiver_git_repo,
    _make_sender_git_repo,
    _write_draft,
)


class TestCompletionClaims:
    def test_bare_claim_flagged(self):
        assert _completion_claims("The HRN mapping is done and wired.\n") == ["done", "wired"]

    def test_case_insensitive_and_distinct(self):
        assert _completion_claims("Done. DONE. Handled.\n") == ["done", "handled"]

    def test_whole_word_only(self):
        assert _completion_claims("abandoned, undone, landed-ish unlandedness\n") == ["landed"]
        assert _completion_claims("abandoned undone wiredness\n") == []

    def test_no_words_empty(self):
        assert _completion_claims("Plain prose.\n") == []

    def test_word_inside_fence_ignored(self):
        body = "Status.\n```\nstatus: done\n```\n"
        assert _completion_claims(body) == []

    def test_command_and_output_block_clears_claim(self):
        body = "It is wired.\n\n```\n$ census --check\nOK 3/3\n```\n"
        assert _completion_claims(body) == []

    def test_single_line_block_does_not_clear_claim(self):
        body = "It is wired.\n\n```\n$ census --check\n```\n"
        assert _completion_claims(body) == ["wired"]

    def test_unclosed_two_line_block_clears_claim(self):
        body = "Handled.\n```\n$ cmd\noutput\n"
        assert _completion_claims(body) == []


@pytest.fixture()
def sender_and_receiver(tmp_path, monkeypatch):
    sender_repo = _make_sender_git_repo(tmp_path)
    receiver_repo = _make_receiver_git_repo(tmp_path)
    claude_home = _make_claude_home(tmp_path, {"project_rag": receiver_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    return sender_repo, receiver_repo


@pytest.mark.cadence
@pytest.mark.spawns_process
class TestOpAdvisory:
    def test_claim_without_block_advises_and_still_delivers(self, sender_and_receiver):
        sender_repo, receiver_repo = sender_and_receiver
        _write_draft(sender_repo, "claim-topic", body="HRN is handled and wired.\n")
        result = _memo_send({"dry_run": False, "topic": "claim-topic"}, repo_root=sender_repo)
        assert result["exit_code"] == 0, result
        acted = result["acted"][0]
        assert acted["completion_claims"] == ["handled", "wired"]
        assert "command-and-output" in acted["completion_advisory"]
        assert acted["written"] is True

    def test_claim_with_command_and_output_not_advised(self, sender_and_receiver):
        sender_repo, receiver_repo = sender_and_receiver
        body = "HRN is wired.\n\n```\n$ census\nOK\n```\n"
        _write_draft(sender_repo, "proof-topic", body=body)
        result = _memo_send({"dry_run": False, "topic": "proof-topic"}, repo_root=sender_repo)
        assert result["exit_code"] == 0, result
        assert "completion_claims" not in result["acted"][0]
