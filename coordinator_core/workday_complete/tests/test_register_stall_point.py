"""workday-complete carries the requirement-register stall point only when the scan finds a stall."""

from __future__ import annotations

from coordinator_core.ops.requirement_register import StallReport
from coordinator_core.workday_complete import brief as wc_brief

_GOALS = {"today": [], "stale": [], "unreadable_error": None}
_DIRTY = {"ambiguous": False, "evidence": "synthetic: clean"}
_ID = "jp_requirement_register_stall"


def _points(report):
    return wc_brief._build_judgment_points(_GOALS, _DIRTY, register_stall=report)


def test_names_stale_stuck_and_unclaimed():
    report = StallReport(
        stale_rows=["state/sizings/a.yaml row R1 (open, claimed by p1, no progress since 2026-01-01)"],
        stuck_plans=["docs/plans/p1.md (executing, untouched 9d, sizing state/sizings/a.yaml)"],
        unclaimed_rows=["state/sizings/a.yaml row R2 (api)"],
    )
    point = {p["id"]: p for p in _points(report)}[_ID]
    assert point["reportable"] is True
    evidence = point["evidence"]
    assert "row R1" in evidence
    assert "docs/plans/p1.md" in evidence
    assert "row R2" in evidence


def test_absent_when_report_empty_or_omitted():
    assert _ID not in {p["id"] for p in _points(StallReport())}
    assert _ID not in {p["id"] for p in _points(None)}
    assert _ID not in {p["id"] for p in wc_brief._build_judgment_points(_GOALS, _DIRTY)}


def test_brief_surfaces_the_point_in_narration(monkeypatch):
    monkeypatch.setattr(
        wc_brief,
        "resolve_operator_config",
        lambda env=None: {"settings_home": "", "claude_klabauter_bin": "", "claude_klabauter_root": "", "content_root": ""},
    )
    monkeypatch.setattr(wc_brief, "_compute_open_day_goals", lambda: _GOALS)
    monkeypatch.setattr(wc_brief, "_compute_dirty_tree_verdict", lambda: _DIRTY)
    monkeypatch.setattr(wc_brief, "head_branch", lambda _p: "work/x")
    monkeypatch.setattr(
        wc_brief,
        "_compute_register_stall",
        lambda: StallReport(unclaimed_rows=["state/sizings/a.yaml row R2 (api)"]),
    )
    code, envelope = wc_brief.brief(decisions={})
    assert code == 0
    assert _ID not in {p["id"] for p in envelope["judgment_points"]}
    assert _ID in envelope["narration"]
