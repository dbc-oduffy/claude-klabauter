"""Unit tests for `coordinator/bin/lib/pm_brief.py` -- resolve, extract, render, digest, trace.

Covers: each of the three sources winning in resolution order, both baton tiers (state and
archive), the ambiguous-within-a-tier refusal, CRLF input, the refusal message naming all three
sources, `trace_ok` true/false, and a CRLF-checkout vs LF-checkout digest of the same brief text
producing an equal `pm-brief-sha`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BIN_LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
if str(_BIN_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_BIN_LIB_DIR))

import pm_brief  # noqa: E402


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- extract_brief_section -------------------------------------------------------------------


def test_extract_brief_section_reads_body_under_heading() -> None:
    plan_text = (
        "---\ntitle: x\n---\n\n"
        "# Title\n\n## PM brief\n\n> line one\n> line two\n\n## Problem\n\nnot the brief\n"
    )
    section = pm_brief.extract_brief_section(plan_text)
    assert section == "line one\nline two"


def test_extract_brief_section_absent_returns_none() -> None:
    assert pm_brief.extract_brief_section("# Title\n\n## Problem\n\ntext\n") is None


def test_extract_brief_section_normalises_crlf() -> None:
    plan_text = "## PM brief\r\n\r\n> crlf words\r\n\r\n## Problem\r\n"
    assert pm_brief.extract_brief_section(plan_text) == "crlf words"


def test_extract_brief_section_reaches_end_of_file() -> None:
    plan_text = "## PM brief\n\n> last section, no trailing heading\n"
    assert pm_brief.extract_brief_section(plan_text) == "last section, no trailing heading"


# --- resolve: source 1, plan's own section ------------------------------------------------------


def test_resolve_prefers_plan_section(tmp_path: Path) -> None:
    plan = _write(
        tmp_path / "docs" / "plans" / "p.md",
        "---\ntitle: x\nexecution_authorized_note: fallback note\n---\n\n"
        "## PM brief\n\n> the real brief\n\n## Problem\n",
    )
    brief = pm_brief.resolve(plan, tmp_path)
    assert brief.text == "the real brief"
    assert brief.source == pm_brief.SOURCE_PM_VERBATIM


def test_resolve_plan_section_carries_ref_from_frontmatter(tmp_path: Path) -> None:
    plan = _write(
        tmp_path / "docs" / "plans" / "p.md",
        "---\ntitle: x\npm_brief:\n  source: pm-verbatim\n  ref: null\n---\n\n"
        "## PM brief\n\n> the real brief\n",
    )
    brief = pm_brief.resolve(plan, tmp_path)
    assert brief.ref is None


# --- resolve: source 2, execution_authorized_note -----------------------------------------------


def test_resolve_falls_back_to_execution_authorized_note(tmp_path: Path) -> None:
    plan = _write(
        tmp_path / "docs" / "plans" / "p.md",
        "---\ntitle: x\nexecution_authorized_note: do the thing\n---\n\n## Problem\n\ntext\n",
    )
    brief = pm_brief.resolve(plan, tmp_path)
    assert brief.text == "do the thing"
    assert brief.source == pm_brief.SOURCE_EXECUTION_UTTERANCE
    assert brief.ref is None


# --- resolve: source 3, baton via predecessor_handoff -------------------------------------------


def test_resolve_falls_back_to_predecessor_handoff(tmp_path: Path) -> None:
    handoff = _write(
        tmp_path / "state" / "handoffs" / "h.md",
        "---\nsummary: the baton summary\n---\n\n## What this covers\n\nbaton body text\n",
    )
    plan = _write(
        tmp_path / "docs" / "plans" / "p.md",
        f"---\ntitle: x\npredecessor_handoff: {handoff.relative_to(tmp_path).as_posix()}\n---\n\n"
        "## Problem\n\ntext\n",
    )
    brief = pm_brief.resolve(plan, tmp_path)
    assert brief.source == pm_brief.SOURCE_BATON_ORIGINATING_ASK
    assert "the baton summary" in brief.text
    assert "baton body text" in brief.text
    assert brief.ref == "state/handoffs/h.md"


# --- resolve: source 3, baton via deliverable_id, both tiers ------------------------------------


def test_resolve_matches_deliverable_id_in_state_tier(tmp_path: Path) -> None:
    _write(
        tmp_path / "state" / "handoffs" / "h.md",
        "---\ndeliverable_id: dlv-abc\nsummary: state tier summary\n---\n",
    )
    plan = _write(
        tmp_path / "docs" / "plans" / "p.md",
        '---\ntitle: x\ndeliverable_id: "dlv-abc"\n---\n\n## Problem\n\ntext\n',
    )
    brief = pm_brief.resolve(plan, tmp_path)
    assert brief.source == pm_brief.SOURCE_BATON_ORIGINATING_ASK
    assert "state tier summary" in brief.text
    assert brief.ref == "state/handoffs/h.md"


def test_resolve_matches_deliverable_id_in_archive_tier_when_state_absent(tmp_path: Path) -> None:
    _write(
        tmp_path / "archive" / "handoffs" / "2026-09" / "h.md",
        "---\ndeliverable_id: dlv-xyz\nsummary: archive tier summary\n---\n",
    )
    plan = _write(
        tmp_path / "docs" / "plans" / "p.md",
        '---\ntitle: x\ndeliverable_id: "dlv-xyz"\n---\n\n## Problem\n\ntext\n',
    )
    brief = pm_brief.resolve(plan, tmp_path)
    assert brief.source == pm_brief.SOURCE_BATON_ORIGINATING_ASK
    assert "archive tier summary" in brief.text


def test_resolve_state_tier_wins_over_archive_tier(tmp_path: Path) -> None:
    _write(
        tmp_path / "state" / "handoffs" / "h.md",
        "---\ndeliverable_id: dlv-both\nsummary: state wins\n---\n",
    )
    _write(
        tmp_path / "archive" / "handoffs" / "h.md",
        "---\ndeliverable_id: dlv-both\nsummary: archive loses\n---\n",
    )
    plan = _write(
        tmp_path / "docs" / "plans" / "p.md",
        '---\ntitle: x\ndeliverable_id: "dlv-both"\n---\n\n## Problem\n\ntext\n',
    )
    brief = pm_brief.resolve(plan, tmp_path)
    assert "state wins" in brief.text


def test_resolve_ambiguous_within_state_tier_is_unresolvable(tmp_path: Path) -> None:
    _write(
        tmp_path / "state" / "handoffs" / "h1.md",
        "---\ndeliverable_id: dlv-dup\nsummary: one\n---\n",
    )
    _write(
        tmp_path / "state" / "handoffs" / "h2.md",
        "---\ndeliverable_id: dlv-dup\nsummary: two\n---\n",
    )
    plan = _write(
        tmp_path / "docs" / "plans" / "p.md",
        '---\ntitle: x\ndeliverable_id: "dlv-dup"\n---\n\n## Problem\n\ntext\n',
    )
    with pytest.raises(pm_brief.NoPmBriefError):
        pm_brief.resolve(plan, tmp_path)


# --- resolve: nothing resolves -------------------------------------------------------------------


def test_resolve_refuses_naming_all_three_sources(tmp_path: Path) -> None:
    plan = _write(
        tmp_path / "docs" / "plans" / "p.md",
        "---\ntitle: x\n---\n\n## Problem\n\ntext, no brief anywhere\n",
    )
    with pytest.raises(pm_brief.NoPmBriefError) as excinfo:
        pm_brief.resolve(plan, tmp_path)
    message = str(excinfo.value)
    assert "PM brief" in message
    assert "execution_authorized_note" in message
    assert "predecessor_handoff" in message or "deliverable_id" in message
    assert "--utterance" in message


# --- digest ----------------------------------------------------------------------------------


def test_digest_is_eight_hex_chars() -> None:
    brief = pm_brief.Brief(text="some words", source=pm_brief.SOURCE_PM_VERBATIM, ref=None)
    d = pm_brief.digest(brief)
    assert len(d) == 8
    int(d, 16)  # raises ValueError if not hex


def test_digest_equal_across_crlf_and_lf_checkout(tmp_path: Path) -> None:
    lf_plan = _write(
        tmp_path / "lf.md", "## PM brief\n\n> same words, two checkouts\n\n## Problem\n"
    )
    crlf_plan = _write(
        tmp_path / "crlf.md",
        "## PM brief\r\n\r\n> same words, two checkouts\r\n\r\n## Problem\r\n",
    )
    lf_section = pm_brief.extract_brief_section(lf_plan.read_text(encoding="utf-8"))
    crlf_section = pm_brief.extract_brief_section(crlf_plan.read_text(encoding="utf-8"))
    lf_brief = pm_brief.Brief(text=lf_section, source=pm_brief.SOURCE_PM_VERBATIM, ref=None)
    crlf_brief = pm_brief.Brief(text=crlf_section, source=pm_brief.SOURCE_PM_VERBATIM, ref=None)
    assert pm_brief.digest(lf_brief) == pm_brief.digest(crlf_brief)


# --- render_block ------------------------------------------------------------------------------


def test_render_block_executor_contains_heading_sha_framing_text_source() -> None:
    brief = pm_brief.Brief(text="do the specific thing", source=pm_brief.SOURCE_PM_VERBATIM, ref=None)
    block = pm_brief.render_block(brief, "executor")
    assert "## PM intent (verbatim)" in block
    assert f"pm-brief-sha: {pm_brief.digest(brief)}" in block
    assert "Do not widen scope to it" in block
    assert "do the specific thing" in block
    assert f"Source: {pm_brief.SOURCE_PM_VERBATIM} (null)" in block


def test_render_block_reviewer_uses_reviewer_framing() -> None:
    brief = pm_brief.Brief(text="do the thing", source=pm_brief.SOURCE_PM_VERBATIM, ref=None)
    block = pm_brief.render_block(brief, "reviewer")
    assert "Judge this diff against the PM's ask" in block
    assert "Do not widen scope to it" not in block


def test_render_block_rejects_unknown_audience() -> None:
    brief = pm_brief.Brief(text="x", source=pm_brief.SOURCE_PM_VERBATIM, ref=None)
    with pytest.raises(ValueError):
        pm_brief.render_block(brief, "narrator")


# --- trace_ok ---------------------------------------------------------------------------------


def test_trace_ok_true_for_substring() -> None:
    assert pm_brief.trace_ok("never the PM's words", "Executors never the PM's words, ever.")


def test_trace_ok_true_ignoring_whitespace_differences() -> None:
    assert pm_brief.trace_ok("never   the PM's\nwords", "never the PM's words")


def test_trace_ok_false_for_paraphrase() -> None:
    assert not pm_brief.trace_ok("a paraphrase of the brief", "the actual verbatim brief text")


def test_trace_ok_false_for_empty_trace() -> None:
    assert not pm_brief.trace_ok("", "some brief text")
    assert not pm_brief.trace_ok(None, "some brief text")
