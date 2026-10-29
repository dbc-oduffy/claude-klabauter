"""A summary memo.compose derives must survive memo.send's cap check on read-back."""

from coordinator_core.ops.fleet._memo_summary import (
    _SUMMARY_MAX_CHARS,
    derive_prose_summary,
    summary_measure,
    validate_explicit_summary,
)
from coordinator_core.ops.fleet.memo_compose import compose_draft_frontmatter
from coordinator_core.ops.fleet.memo_send import parse_frontmatter

_QUOTED = 'Engine run halted at stage with: "' + "reader did not run " * 20 + '" end.'


def _roundtrip(summary: str) -> str:
    fm = compose_draft_frontmatter(
        from_id="a", to="b", title="t", today="2026-10-28", summary=summary, kind="bug"
    )
    return parse_frontmatter(fm + "\nbody\n")["frontmatter"]["summary"]


def test_derived_quoted_summary_within_cap_as_send_reads_it():
    derived = derive_prose_summary(_QUOTED)
    assert derived.endswith("…")
    assert len(_roundtrip(derived)) <= _SUMMARY_MAX_CHARS
    assert validate_explicit_summary("send", _roundtrip(derived)) is None


def test_plain_summary_still_fills_cap():
    derived = derive_prose_summary("x" * 500)
    assert len(derived) == _SUMMARY_MAX_CHARS


def test_short_quoted_summary_untouched_and_over_cap_still_refused():
    assert derive_prose_summary('Say "hi".') == 'Say "hi".'
    over = '"' * (_SUMMARY_MAX_CHARS // 2 + 1)
    assert summary_measure(over) > _SUMMARY_MAX_CHARS
    assert validate_explicit_summary("compose", over) is not None
    assert validate_explicit_summary("send", "y" * (_SUMMARY_MAX_CHARS + 1)) is not None
