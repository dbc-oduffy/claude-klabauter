"""memo.send advises (never holds) when the body claims completion — done /
landed / wired / mapped / handled — without a fenced command-and-output block.

Unit tests exercise `_completion_claims`; the op-level test drives `_memo_send`
and reads `completion_claims` off the acted item.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.fleet.memo_send import _completion_claims


class TestCompletionClaims:
    def test_bare_claim_flagged(self) -> None:
        assert _completion_claims("The HRN mapping is done and wired.\n") == ["done", "wired"]

    def test_case_insensitive_and_distinct(self) -> None:
        assert _completion_claims("Done. DONE. Handled.\n") == ["done", "handled"]

    def test_whole_word_only(self) -> None:
        assert _completion_claims("abandoned, undone, landed-ish unlandedness\n") == ["landed"]
        assert _completion_claims("abandoned undone wiredness\n") == []

    def test_no_words_empty(self) -> None:
        assert _completion_claims("Plain prose.\n") == []

    def test_word_inside_fence_ignored(self) -> None:
        body = "Status.\n```\nstatus: done\n```\n"
        assert _completion_claims(body) == []

    def test_command_and_output_block_clears_claim(self) -> None:
        body = "It is wired.\n\n```\n$ census --check\nOK 3/3\n```\n"
        assert _completion_claims(body) == []

    def test_single_line_block_does_not_clear_claim(self) -> None:
        body = "It is wired.\n\n```\n$ census --check\n```\n"
        assert _completion_claims(body) == ["wired"]

    def test_unclosed_two_line_block_clears_claim(self) -> None:
        body = "Handled.\n```\n$ cmd\noutput\n"
        assert _completion_claims(body) == []
