"""An over-cap composed brief ends at a whole sentence and names the full plan text."""

from coordinator_core.ops.dispatch_emit.emit import (
    _EXECUTION_NOTES_CHAR_CAP,
    _PLAN_CONTEXT_PREAMBLE_CHAR_CAP,
    PlanContext,
    _capped_execution_notes,
    _plan_context_preamble,
)
from coordinator_core.ops.dispatch_emit.row_excerpts import cut_whole, excerpt_block

_SENTENCE = "Reduced-scale decode is optional until the memory budget is measured. "
_MARK = "(cut at a whole sentence"


def _body_before_marker(text: str) -> str:
    return text.split("\n" + _MARK)[0]


def test_under_cap_text_is_unchanged():
    assert cut_whole("One sentence.", 100, "p") == "One sentence."


def test_over_cap_cut_ends_on_a_sentence_and_names_the_pointer():
    text = _SENTENCE * 30
    out = cut_whole(text, 300, "`p.md` row `C1`")
    assert len(out) <= 300
    head = _body_before_marker(out)
    assert head.endswith("measured.")
    assert text.startswith(head)
    assert "`p.md` row `C1`" in out


def test_cut_prefers_the_later_of_sentence_and_line_boundary():
    text = "First sentence ends here.\n- a bullet with no full stop\n" + "x" * 300
    head = _body_before_marker(cut_whole(text, 130, "p"))
    assert head.endswith("no full stop")


def test_byte_measure_counts_multibyte_characters():
    text = "Ünïcödé sentence. " * 40
    out = cut_whole(text, 200, "p", size=lambda s: len(s.encode("utf-8")))
    assert len(out.encode("utf-8")) <= 200
    assert _body_before_marker(out).endswith("sentence.")


def test_preamble_cut_is_a_whole_sentence_with_plan_pointer():
    ctx = PlanContext(
        title="T",
        goal="G.",
        problem_excerpt=_SENTENCE * 40,
        plan_path="docs/plans/p.md",
    )
    out = _plan_context_preamble(ctx)
    assert len(out) <= _PLAN_CONTEXT_PREAMBLE_CHAR_CAP
    assert _body_before_marker(out).endswith("measured.")
    assert "`docs/plans/p.md` `## Goal` and `## Problem`" in out
    assert "…" not in out


def test_preamble_under_cap_is_unchanged():
    out = _plan_context_preamble(PlanContext(title="T", goal="G.", problem_excerpt="P."))
    assert _MARK not in out


def test_execution_notes_cut_is_a_whole_sentence_with_plan_pointer():
    plan = "## Execution notes\n\n" + _SENTENCE * 100 + "\n\n## Tasks\n\nx\n"
    out = _capped_execution_notes(plan, "docs/plans/p.md")
    assert len(out) <= _EXECUTION_NOTES_CHAR_CAP
    assert _body_before_marker(out).endswith("measured.")
    assert "`docs/plans/p.md` `## Execution notes`" in out


def test_execution_notes_under_cap_are_verbatim():
    plan = "## Execution notes\n\nCompile first.\n\n## Tasks\n\nx\n"
    assert _capped_execution_notes(plan) == "Compile first."


def test_row_excerpt_cut_names_plan_and_row_id(tmp_path):
    out = excerpt_block(_SENTENCE * 40, [], tmp_path, "h", {}, "docs/plans/p.md", "C1")
    assert _body_before_marker(out).endswith("measured.")
    assert "`docs/plans/p.md` row `C1`" in out


def test_word_fallback_cut_names_a_word_boundary_not_a_sentence():
    out = cut_whole("word " * 200, 120, "p")
    assert "(cut at a word boundary; full text: p)" in out
    assert "whole sentence" not in out
    assert len(out) <= 120
