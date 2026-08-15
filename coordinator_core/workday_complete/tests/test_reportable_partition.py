"""coordinator_core.workday_complete.tests.test_reportable_partition — C3
(docs/plans/2026-08-15-judgment-points-that-gate-nothing-stop-being-
questions.md): `workday-complete`'s `_build_judgment_points` /
`_build_day_goal_closeout_judgment_point` output, routed through C1's
`partition_reportable` and folded into `narration`.

Classifies by the predicate, never by a hard-coded id list -- the four
known-reported ids (`jp_step4b_analyst_dispatch`, `jp_step4c_observer_dispatch`,
`jp_step4_5_clustering_dispatch`, `jp_step4e_health_ledger_new_rows`) are
asserted as a CHECK on the predicate's live output, not fed into it.

Run scoped only:
    python3 -m pytest coordinator_core/workday_complete/tests/test_reportable_partition.py -q
"""

from __future__ import annotations

from coordinator_core.contract.decision_object.judgment import partition_reportable
from coordinator_core.workday_complete import brief as wc_brief

_NONEMPTY_OPEN_DAY_GOALS = {
    "today": [{"goal_id": "g_today", "text": "ship the thing"}],
    "stale": [{"goal_id": "g_stale", "text": "stale thing"}],
    "unreadable_error": None,
}
_EMPTY_OPEN_DAY_GOALS = {"today": [], "stale": [], "unreadable_error": None}
_DIRTY_TREE = {"ambiguous": True, "evidence": "synthetic: ambiguous paths remain"}

_KNOWN_REPORTED_IDS = {
    "jp_step4b_analyst_dispatch",
    "jp_step4c_observer_dispatch",
    "jp_step4_5_clustering_dispatch",
    "jp_step4e_health_ledger_new_rows",
}


def _build_all(open_day_goals: dict, dirty_tree_verdict: dict):
    directives = wc_brief._build_directives({}, open_day_goals, dirty_tree_verdict)
    judgment_points = wc_brief._build_judgment_points(open_day_goals, dirty_tree_verdict)
    return partition_reportable(judgment_points, directives)


def test_predicate_demotes_exactly_the_four_known_gate_nothing_ids():
    """With every conditional point ALSO present (nonempty open day goals,
    ambiguous dirty tree), the predicate's `reported` set must equal the
    plan's four known ids exactly -- neither more nor fewer."""
    asked, reported = _build_all(_NONEMPTY_OPEN_DAY_GOALS, _DIRTY_TREE)
    reported_ids = {p["id"] for p in reported}
    assert reported_ids == _KNOWN_REPORTED_IDS
    asked_ids = {p["id"] for p in asked}
    assert asked_ids == {
        "jp_day_goal_closeout",
        "jp_step2_5_dirty_tree_ambiguous",
        "jp_step3_5_backfill_cap",
    }


def test_asked_points_gate_a_directive_present_on_the_envelope():
    """Every asked point's resolves-target must actually be a directive id
    present in this run's directives[] -- the shape of the invariant, not a
    restatement of the id list."""
    directives = wc_brief._build_directives({}, _NONEMPTY_OPEN_DAY_GOALS, _DIRTY_TREE)
    directive_ids = {d["id"] for d in directives}
    asked, _ = _build_all(_NONEMPTY_OPEN_DAY_GOALS, _DIRTY_TREE)
    for point in asked:
        resolves_ids = {
            rid
            for disposition in point.get("dispositions") or []
            for rid in (disposition.get("resolves") or [])
        }
        assert resolves_ids & directive_ids, point["id"]


def test_reported_points_gate_no_directive_present_on_the_envelope():
    directives = wc_brief._build_directives({}, _NONEMPTY_OPEN_DAY_GOALS, _DIRTY_TREE)
    directive_ids = {d["id"] for d in directives}
    _, reported = _build_all(_NONEMPTY_OPEN_DAY_GOALS, _DIRTY_TREE)
    for point in reported:
        resolves_ids = {
            rid
            for disposition in point.get("dispositions") or []
            for rid in (disposition.get("resolves") or [])
        }
        assert not (resolves_ids & directive_ids), point["id"]


def test_reported_narration_suffix_renders_question_and_rationale():
    _, reported = _build_all(_NONEMPTY_OPEN_DAY_GOALS, _DIRTY_TREE)
    suffix = wc_brief._reported_narration_suffix(reported)
    assert "4 point(s) gate nothing on this run and are reported, not asked:" in suffix
    assert "jp_step4b_analyst_dispatch (" in suffix
    assert "Step 4b's Sonnet analyst is the primary Work Completed source" in suffix


def test_reported_narration_suffix_empty_when_nothing_reported():
    assert wc_brief._reported_narration_suffix([]) == ""


def _stub_operator_config(monkeypatch) -> None:
    """Mirrors `test_brief_goal_close_day.py`'s own helper -- `brief()`
    calls `resolve_operator_config` before anything else, which fails to
    resolve under the suite-root HOME quarantine."""
    monkeypatch.setattr(
        wc_brief,
        "resolve_operator_config",
        lambda env=None: {
            "settings_home": "",
            "claude_klabauter_bin": "",
            "claude_klabauter_root": "",
            "doe_root": "",
        },
    )


def test_brief_end_to_end_moves_reported_points_out_of_judgment_points(monkeypatch):
    """`brief()`'s own emitted envelope must reflect the same partition --
    the four known ids absent from `judgment_points[]`, present in
    `narration` instead."""
    _stub_operator_config(monkeypatch)
    monkeypatch.setattr(wc_brief, "_compute_open_day_goals", lambda: _EMPTY_OPEN_DAY_GOALS)
    monkeypatch.setattr(
        wc_brief,
        "_compute_dirty_tree_verdict",
        lambda: {"ambiguous": False, "evidence": "synthetic: clean"},
    )
    exit_code, envelope = wc_brief.brief(decisions={})
    assert exit_code == 0
    jp_ids = {p["id"] for p in envelope["judgment_points"]}
    assert jp_ids.isdisjoint(_KNOWN_REPORTED_IDS)
    assert "jp_step4b_analyst_dispatch (" in envelope["narration"]
    assert "point(s) gate nothing on this run and are reported, not asked" in envelope["narration"]
