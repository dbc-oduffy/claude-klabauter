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


# --- sizing pm_verbatims: resolve step 2, render helper, from-sizing CLI ---------------------

_SIZING_REL = "state/sizings/s.yaml"


def _sizing(tmp_path: Path, body: str, newline: str = "\n") -> Path:
    path = tmp_path / _SIZING_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.replace("\n", newline).encode("utf-8"))
    return path


_VERBATIMS = (
    "intent: x\npm_verbatims:\n"
    "  - turn: 4\n    text: |-\n      newest ask\n      second line\n"
    "  - turn: 1\n    text: older ask\n    truncated: true\n"
)


def _plan(tmp_path: Path, extra_fm: str = "", body: str = "## Problem\n\ntext\n") -> Path:
    return _write(
        tmp_path / "plan.md",
        f'---\ntitle: x\nsizing_object: "{_SIZING_REL}"\n{extra_fm}---\n\n{body}',
    )


def test_resolve_uses_sizing_verbatims_when_no_body_section(tmp_path: Path) -> None:
    _sizing(tmp_path, _VERBATIMS)
    brief = pm_brief.resolve(_plan(tmp_path), tmp_path)
    assert brief.source == pm_brief.SOURCE_PM_VERBATIM
    assert brief.ref == _SIZING_REL
    assert brief.text == "newest ask\nsecond line\n\nolder ask\n...[truncated]"


def test_resolve_sizing_order_is_file_order_and_crlf_normalised(tmp_path: Path) -> None:
    _sizing(tmp_path, _VERBATIMS, newline="\r\n")
    brief = pm_brief.resolve(_plan(tmp_path), tmp_path)
    assert brief.text.index("newest ask") < brief.text.index("older ask")
    assert "\r" not in brief.text


def test_resolve_sizing_without_field_falls_through(tmp_path: Path) -> None:
    _sizing(tmp_path, "intent: x\n")
    plan = _plan(tmp_path, extra_fm="execution_authorized_note: fallback\n")
    brief = pm_brief.resolve(plan, tmp_path)
    assert brief.source == pm_brief.SOURCE_EXECUTION_UTTERANCE


def test_resolve_missing_sizing_file_falls_through(tmp_path: Path) -> None:
    plan = _plan(tmp_path, extra_fm="execution_authorized_note: fallback\n")
    assert pm_brief.resolve(plan, tmp_path).text == "fallback"


def test_resolve_body_section_beats_sizing(tmp_path: Path) -> None:
    _sizing(tmp_path, _VERBATIMS)
    plan = _plan(tmp_path, body="## PM brief\n\n> body words\n\n## Problem\n")
    brief = pm_brief.resolve(plan, tmp_path)
    assert brief.text == "body words"
    assert brief.ref is None


def test_render_sizing_brief_blockquote_round_trips_through_extract(tmp_path: Path) -> None:
    sizing = _sizing(tmp_path, _VERBATIMS)
    rendered = pm_brief.render_sizing_brief(sizing)
    assert rendered is not None
    assert rendered.startswith("> newest ask\n> second line\n> \n> older ask\n> ...[truncated]")
    plan_text = f"## PM brief\n\n{rendered}\n\n## Problem\n"
    assert pm_brief.extract_brief_section(plan_text) == pm_brief.resolve(
        _plan(tmp_path), tmp_path
    ).text


def test_render_sizing_brief_none_without_field(tmp_path: Path) -> None:
    assert pm_brief.render_sizing_brief(_sizing(tmp_path, "intent: x\n")) is None
    assert pm_brief.render_sizing_brief(tmp_path / "absent.yaml") is None


def test_from_sizing_cli(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "pm_brief_cli", Path(__file__).resolve().parents[1] / "pm-brief.py"
    )
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    sizing = _sizing(tmp_path, _VERBATIMS)
    assert cli.main(["from-sizing", str(sizing)]) == 0
    assert capsys.readouterr().out.startswith("> newest ask")

    empty = _sizing(tmp_path, "intent: x\n")
    assert cli.main(["from-sizing", str(empty)]) == 1
    assert capsys.readouterr().out == ""


# --- note_carries_pm_words: wordless typed-command notes -------------------------------------

_TYPED = "PM authorized by typed command: /execute-plan (no accompanying words)"
_SIZING = "authorized by accepted sizing (state/sizings/x.yaml)"


@pytest.mark.parametrize(
    "note,expected",
    [
        (_TYPED, False),
        (_SIZING, False),
        (f"{_TYPED}\n{_SIZING}", False),
        ("", False),
        ("handoff for execution", True),
        ("go with your recs. we can execute after handoff", True),
        (f"handoff for execution\n{_TYPED}", True),
    ],
)
def test_note_carries_pm_words(note: str, expected: bool) -> None:
    assert pm_brief.note_carries_pm_words(note) is expected


def _note_plan(tmp_path: Path, note: str) -> Path:
    return _write(
        tmp_path / "docs" / "plans" / "p.md",
        f'---\ntitle: x\nexecution_authorized_note: "{note}"\n---\n\n## Problem\n\ntext\n',
    )


def test_resolve_wordless_note_refuses_naming_utterance(tmp_path: Path) -> None:
    plan = _note_plan(tmp_path, _TYPED)
    with pytest.raises(pm_brief.NoPmBriefError) as excinfo:
        pm_brief.resolve(plan, tmp_path)
    assert "--utterance" in str(excinfo.value)


def test_resolve_wordless_note_falls_through_to_baton(tmp_path: Path) -> None:
    plan = _write(
        tmp_path / "docs" / "plans" / "p.md",
        f'---\ntitle: x\nexecution_authorized_note: "{_TYPED}"\n'
        "predecessor_handoff: state/handoffs/h.md\n---\n\n## Problem\n\ntext\n",
    )
    _write(tmp_path / "state" / "handoffs" / "h.md", "---\nsummary: baton ask\n---\n")
    brief = pm_brief.resolve(plan, tmp_path)
    assert brief.source == pm_brief.SOURCE_BATON_ORIGINATING_ASK


def test_resolve_bare_utterance_note_keeps_resolving(tmp_path: Path) -> None:
    brief = pm_brief.resolve(_note_plan(tmp_path, "handoff for execution"), tmp_path)
    assert brief.source == pm_brief.SOURCE_EXECUTION_UTTERANCE
    assert brief.text == "handoff for execution"


def test_resolve_multiline_note_keeps_only_pm_line(tmp_path: Path) -> None:
    plan = _write(
        tmp_path / "docs" / "plans" / "p.md",
        f"---\ntitle: x\nexecution_authorized_note: |\n  handoff for execution\n  {_TYPED}\n"
        "---\n\n## Problem\n\ntext\n",
    )
    assert pm_brief.resolve(plan, tmp_path).text == "handoff for execution"


@pytest.mark.parametrize("kind", ["abs", "dotdot"])
def test_resolve_ignores_uncontained_sizing_object(tmp_path: Path, kind: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = _write(tmp_path / "outside" / "s.yaml", _VERBATIMS)
    ref = str(outside) if kind == "abs" else "../outside/s.yaml"
    plan = _write(
        repo / f"{kind}.md",
        f'---\ntitle: x\nsizing_object: "{ref}"\n---\n\n## Problem\n\ntext\n',
    )
    with pytest.raises(pm_brief.NoPmBriefError):
        pm_brief.resolve(plan, repo)
