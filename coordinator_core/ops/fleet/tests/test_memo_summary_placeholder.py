"""
Tests for coordinator_core.ops.fleet._memo_summary's C1 additions:
SUMMARY_PLACEHOLDER, is_placeholder_summary, validate_explicit_summary.

Spec backlink: pln-memo-summary-cap-warn-at-draft-8246d3 § C1
"""

from __future__ import annotations

from coordinator_core.ops.fleet.memo_draft import _BODY_PLACEHOLDER
from coordinator_core.ops.fleet._memo_summary import (
    SUMMARY_PLACEHOLDER,
    _SUMMARY_MAX_CHARS,
    derive_prose_summary,
    has_prose_body,
    is_placeholder_summary,
    validate_explicit_summary,
)


# ---------------------------------------------------------------------------
# has_prose_body / multi-line comment stripping
#
# Regression, DoE-claude 2026-08-30: a memo reached them as frontmatter plus
# four empty comment blocks, `summary:` holding a fragment of the draft warning
# itself. Two causes, both here: the comment predicate was anchored per-line so
# multi-line blocks leaked their interiors as "prose", and nothing on the send
# path ever asked whether the body had prose in it at all.
# ---------------------------------------------------------------------------

def test_draft_placeholder_body_has_no_prose():
    assert has_prose_body(_BODY_PLACEHOLDER) is False


def test_draft_placeholder_body_derives_no_summary():
    # The interior of a spanning comment must never become a memo's summary.
    assert derive_prose_summary(_BODY_PLACEHOLDER) == ""


def test_empty_and_blank_bodies_have_no_prose():
    assert has_prose_body("") is False
    assert has_prose_body("\n\n   \n") is False


def test_heading_only_body_has_no_prose():
    assert has_prose_body("# A heading\n\n## Another\n") is False


def test_multi_line_comment_interior_is_not_prose():
    body = "<!-- opening line\n     interior line that is not prose\n     closing -->\n"
    assert has_prose_body(body) is False


def test_unterminated_comment_does_not_leak_its_tail():
    # An unterminated `<!--` swallows the rest of the body rather than leaking
    # everything after it as prose.
    assert has_prose_body("<!-- opened and never closed\nstill inside\n") is False


def test_prose_after_a_spanning_comment_is_found():
    body = "<!-- a note\n     spanning lines -->\n\nThe actual ask. More after it.\n"
    assert has_prose_body(body) is True
    assert derive_prose_summary(body) == "The actual ask."


def test_prose_body_is_prose():
    assert has_prose_body("A memo with something in it.\n") is True


def test_placeholder_is_exactly_99_chars():
    # The ruler's own prose claims "99 characters" — assert it rather than
    # trust it (plan C1: "a ruler that lies about its own length is worse
    # than no ruler").
    assert len(SUMMARY_PLACEHOLDER) == 99


def test_placeholder_has_double_space_after_first_sentence():
    # The double space is what makes the count come out — pin it so a future
    # whitespace-normalizing edit is caught here rather than silently
    # shortening the ruler by one char.
    assert "characters.  this is 99" in SUMMARY_PLACEHOLDER


def test_is_placeholder_summary_true_for_exact_match():
    assert is_placeholder_summary(SUMMARY_PLACEHOLDER) is True


def test_is_placeholder_summary_true_after_stripping_whitespace():
    assert is_placeholder_summary(f"  {SUMMARY_PLACEHOLDER}\n") is True


def test_is_placeholder_summary_true_for_none():
    assert is_placeholder_summary(None) is True


def test_is_placeholder_summary_true_for_empty_string():
    assert is_placeholder_summary("") is True


def test_is_placeholder_summary_false_for_near_miss_quoting_ruler_words():
    # Anti-scope: a real summary that happens to quote the ruler's words
    # must NOT be swallowed by a substring/prefix match.
    near_miss = f"See also: {SUMMARY_PLACEHOLDER}"
    assert is_placeholder_summary(near_miss) is False


def test_is_placeholder_summary_false_for_prefix_of_ruler():
    prefix_only = SUMMARY_PLACEHOLDER[:-1]
    assert is_placeholder_summary(prefix_only) is False


def test_is_placeholder_summary_false_for_ordinary_summary():
    assert is_placeholder_summary("A perfectly ordinary summary.") is False


def test_validate_explicit_summary_none_at_cap_boundary():
    summary_120 = "x" * _SUMMARY_MAX_CHARS
    assert _SUMMARY_MAX_CHARS == 120
    assert validate_explicit_summary("compose", summary_120) is None


def test_validate_explicit_summary_none_at_119_chars():
    summary_119 = "x" * 119
    assert validate_explicit_summary("compose", summary_119) is None


def test_validate_explicit_summary_error_at_121_chars():
    summary_121 = "x" * 121
    error = validate_explicit_summary("compose", summary_121)
    assert error is not None
    assert "121 chars" in error
    assert "cap is 120" in error


def test_validate_explicit_summary_none_when_summary_is_none():
    assert validate_explicit_summary("draft", None) is None


def test_validate_explicit_summary_draft_message_wording():
    over_cap = "x" * 121
    error = validate_explicit_summary("draft", over_cap)
    assert error == (
        "memo.draft: summary is 121 chars, cap is 120 — shorten it or omit "
        "summary and let memo.compose derive one from the body instead"
    )


def test_validate_explicit_summary_compose_message_wording():
    over_cap = "x" * 121
    error = validate_explicit_summary("compose", over_cap)
    assert error == (
        "memo.compose: summary is 121 chars, cap is 120 — shorten it or "
        "omit summary to derive one from the body instead"
    )


def test_validate_explicit_summary_send_message_wording():
    over_cap = "x" * 121
    error = validate_explicit_summary("send", over_cap)
    assert error == "memo.send: summary is 121 chars, cap is 120 — shorten it"


def test_validate_explicit_summary_send_backstop_message_wording():
    over_cap = "x" * 121
    error = validate_explicit_summary("send_backstop", over_cap)
    assert error == (
        "memo.send: summary is 121 chars, cap is 120 — shorten it or omit "
        "summary to derive one from the body instead"
    )
