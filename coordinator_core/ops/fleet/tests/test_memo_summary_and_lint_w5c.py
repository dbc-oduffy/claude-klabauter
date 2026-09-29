"""Summary-cap wording and sender-qualified citation lint (coordinator-content-repo #111)."""

from __future__ import annotations

from coordinator_core.ops.fleet._memo_summary import (
    _SUMMARY_MAX_CHARS,
    SUMMARY_PLACEHOLDER,
    is_placeholder_summary,
    validate_explicit_summary,
)
from coordinator_core.ops.fleet.memo_send import (
    _citation_lint_notice,
    _unqualified_path_citations,
)

_LEGACY = (
    "[Replace me as a summary, no more than 100 characters.  this is 99 "
    "characters, it just so happens!]"
)


def test_placeholder_names_the_enforced_cap():
    assert f"{_SUMMARY_MAX_CHARS} characters" in SUMMARY_PLACEHOLDER


def test_legacy_placeholder_on_existing_drafts_is_still_a_placeholder():
    assert is_placeholder_summary(_LEGACY)
    assert is_placeholder_summary(SUMMARY_PLACEHOLDER)


def test_refusal_states_actual_length_and_cap():
    message = validate_explicit_summary("send", "x" * 130)
    assert "130 chars" in message and f"cap is {_SUMMARY_MAX_CHARS}" in message


def test_lint_notice_names_the_sender_repo_it_qualified_against():
    notice = _citation_lint_notice(["docs/x.md"], "sender-repo")
    assert "sender-repo" in notice
    assert "qualified" in notice


def test_sender_qualified_path_is_not_flagged():
    qualifiers = frozenset({"sender-repo"})
    assert _unqualified_path_citations("see sender-repo docs/x.md", qualifiers) == []
    assert _unqualified_path_citations("see sender-repo:docs/x.md", qualifiers) == []
