"""Check 15 -- trailer-demoted-to-body.

Guards the P1 recorded at ``state/bug-backlog/2026-09-19-a-blank-line-turns-
a-git-trailer-into-bo-964db9e54ea6.yaml``: three chunk commits on 2026-08-21
were written with successive ``-m`` flags --

    -m "..." -m "Deliverable-Id: dlv-..." -m "Co-Authored-By: Claude Opus 5 <...>"

-- each ``-m`` becoming its own blank-line-separated paragraph. ``git`` parses
ONLY the message's final paragraph as trailers, so ``Deliverable-Id`` landed
as body text: present to a human reading ``git log``, invisible to
``git log --format='%(trailers:...)'`` and everything that reads trailers
that way. ``close-out-and-stamp`` reported all eight chunk ids missing over a
range that provably contained every one of them.
"""

from __future__ import annotations

from coordinator_core.bash_guards.commit_tripwires import (
    check_trailer_demoted_to_body,
)


def _tokens(*args: str):
    return ["git", "commit", *args]


# ---------------------------------------------------------------------------
# The incident shape itself.
# ---------------------------------------------------------------------------


def test_fires_on_the_recorded_incident_shape():
    """The 2026-08-21 shape: a subject, then a Deliverable-Id paragraph that
    a blank line separates from the real trailing trailer block."""
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
    """Both a Deliverable-Id AND an earlier Closes-shaped paragraph get
    demoted -- every non-subject, non-terminal paragraph is a candidate, not
    only the one immediately above the real trailer block."""
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


# ---------------------------------------------------------------------------
# Well-formed messages stay silent.
# ---------------------------------------------------------------------------


def test_silent_on_a_single_well_formed_trailing_trailer_block():
    """A subject, then one ``-m`` whose own value already folds every trailer
    into ONE trailing paragraph -- exactly what a correctly-composed commit
    looks like. No blank line separates any trailer from another, so nothing
    is demoted."""
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
    """This repo's own `prefix: description` subject convention (e.g.
    ``bug-blitz: close a P1 row``) matches the same `Token: value` regex a
    real trailer does. The subject is always git's first paragraph, never a
    candidate trailer, however it is shaped -- a two-paragraph message (just
    subject + one trailing block) must never fire on this basis alone."""
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
    """A subject, an explanatory body paragraph, then a trailing trailer
    block -- neither non-terminal paragraph is trailer-shaped, so this is
    the ordinary, unremarkable multi-paragraph commit shape."""
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


# ---------------------------------------------------------------------------
# Fail-open cases -- same posture as Check 14 (advisory only).
# ---------------------------------------------------------------------------


def test_fails_open_when_the_commit_segment_was_not_resolved():
    assert check_trailer_demoted_to_body(None) is None


def test_fails_open_when_the_message_is_not_on_the_command_line():
    assert check_trailer_demoted_to_body(_tokens("-F", "/tmp/msg.txt")) is None
