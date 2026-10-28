"""An unrecorded pm ask and an over-cap brief are admitted with an advisory, never refused."""

from __future__ import annotations

from coordinator_core.ops import baton_pm_turns as pm_turns
from coordinator_core.ops.dispatch_emit.ask_brief import EM_BRIEF_BYTE_CAP, load_em_brief, receipt_fields
from coordinator_core.ops.dispatch_emit.tests.test_unmarked_ask_source_derivation import (  # noqa: F401
    SID,
    TURN,
    _emit,
    _seed,
    repo,
)


def test_pm_ask_matching_no_turn_is_admitted_and_recorded(repo):  # noqa: F811
    _seed(repo)
    said = "unrecorded words that nobody said yet"
    out, _ = _emit(repo, ask=said, ask_source="pm")
    assert out["ask_source"] == "pm" and out["pm_verification"] == "unmatched-admitted"
    assert out["pm_advisory"] and out["pm_turn_recorded"] == 1
    turns = pm_turns.read_turns(session_id=SID, cwd=str(repo), all_turns=True)["turns"]
    assert [t["prompt"] for t in turns] == [TURN, said]


def test_matched_pm_ask_records_nothing_and_carries_no_advisory(repo):  # noqa: F811
    _seed(repo)
    out, _ = _emit(repo, ask=TURN, ask_source="pm")
    assert out["pm_verification"] == "matched" and "pm_advisory" not in out
    assert pm_turns.read_turns(session_id=SID, cwd=str(repo), all_turns=True)["count"] == 1


def test_over_cap_inline_brief_spills_whole_text_with_advisory(tmp_path):
    text = "é" * EM_BRIEF_BYTE_CAP
    b = load_em_brief(tmp_path, brief=text, brief_file=None)
    assert len(b.text.encode("utf-8")) <= EM_BRIEF_BYTE_CAP and text.startswith(b.text)
    assert len(b.context) == 1 and (tmp_path / b.context[0]).read_text(encoding="utf-8") == text
    assert b.advisories and receipt_fields(b)["advisories"] == list(b.advisories)


def test_over_cap_brief_file_is_its_own_spill(tmp_path):
    (tmp_path / "b.md").write_text("a" * (EM_BRIEF_BYTE_CAP + 5), encoding="utf-8")
    b = load_em_brief(tmp_path, brief=None, brief_file="b.md")
    assert b.context == ("b.md",) and b.advisories


def test_under_cap_brief_has_no_advisory(tmp_path):
    b = load_em_brief(tmp_path, brief="short", brief_file=None)
    assert b.advisories == () and "advisories" not in receipt_fields(b)
