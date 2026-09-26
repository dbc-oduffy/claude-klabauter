
from __future__ import annotations

from coordinator_core.bash_guards.commit_tripwires import (
    check_trailer_demoted_to_body,
)


def _tokens(*args: str):
    return ["git", "commit", *args]


def test_fires_on_the_recorded_incident_shape():
    detail = check_trailer_demoted_to_body(
        _tokens(
            "-m",
            "bug-blitz: close a P1 row",
            "-m",
            "Deliverable-Id: dlv-abc123",
            "-m",
            "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>",
        )
    )
    assert detail is not None
    assert "TRAILER DEMOTED TO BODY" in detail
    assert "Deliverable-Id: dlv-abc123" in detail


def test_fires_with_two_trailer_shaped_paragraphs_ahead_of_the_block():
    detail = check_trailer_demoted_to_body(
        _tokens(
            "-m",
            "bug-blitz: close a P1 row",
            "-m",
            "Closes: bug-1234",
            "-m",
            "Deliverable-Id: dlv-abc123",
            "-m",
            "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>",
        )
    )
    assert detail is not None
    assert "Closes: bug-1234" in detail
    assert "Deliverable-Id: dlv-abc123" in detail


def test_silent_on_a_single_well_formed_trailing_trailer_block():
    assert (
        check_trailer_demoted_to_body(
            _tokens(
                "-m",
                "bug-blitz: close a P1 row",
                "-m",
                "Deliverable-Id: dlv-abc123\n"
                "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>",
            )
        )
        is None
    )


def test_silent_when_the_message_is_just_a_subject():
    assert (
        check_trailer_demoted_to_body(_tokens("-m", "a plain subject line"))
        is None
    )


def test_silent_when_the_subject_alone_is_trailer_shaped():
    assert (
        check_trailer_demoted_to_body(
            _tokens(
                "-m",
                "bug-blitz: close a P1 row",
                "-m",
                "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>",
            )
        )
        is None
    )


def test_silent_when_prose_precedes_the_trailer_block():
    assert (
        check_trailer_demoted_to_body(
            _tokens(
                "-m",
                "bug-blitz: close a P1 row",
                "-m",
                "This explains the change in prose, not a trailer.",
                "-m",
                "Session-Id: 159a6a21-19fe-5842-bec7-5efd32bd9f1c",
            )
        )
        is None
    )


def test_fails_open_when_the_commit_segment_was_not_resolved():
    assert check_trailer_demoted_to_body(None) is None


def test_fails_open_when_the_message_is_not_on_the_command_line():
    assert check_trailer_demoted_to_body(_tokens("-F", "/tmp/msg.txt")) is None
