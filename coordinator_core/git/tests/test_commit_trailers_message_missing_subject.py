
from __future__ import annotations

import pytest

from coordinator_core.git.commit_trailers import message_missing_subject


@pytest.mark.parametrize("message", ["", "   ", "\n\n  \n"])
def test_an_empty_or_whitespace_only_message_has_no_subject(message):
    assert message_missing_subject(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "Session-Id: 6ab7b0d8-1234-4a12-9abc-1234567890ab\n",
        "Deliverable-Id: some-deliverable",
        "Co-Authored-By: Claude <noreply@anthropic.com>",
        "Signed-off-by: someone <someone@example.com>",
        "\n\nSession-Id: 6ab7b0d8-1234-4a12-9abc-1234567890ab\n",
    ],
)
def test_a_message_whose_first_line_is_a_trailer_has_no_subject(message):
    assert message_missing_subject(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "fix: the frobnicator",
        "grind(profile): row-1 committed",
        "Refactor session scope\n\nSession-Id: 6ab7b0d8-1234-4a12-9abc-1234567890ab\n",
        "a plain sentence with no colon at all",
        "percolate-round: wiki seed check maps nested renames",
        "hook-run: --advisory makes a failure silent",
    ],
)
def test_an_ordinary_message_has_a_subject(message):
    assert message_missing_subject(message) is False
