
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

_KNOWN_ACTION_CLASS_IDS = {
    "jp_step4b_analyst_dispatch",
    "jp_step4c_observer_dispatch",
    "jp_step4_5_clustering_dispatch",
    "jp_step4e_health_ledger_new_rows",
}


def _build_all(open_day_goals: dict, dirty_tree_verdict: dict):
    directives = wc_brief._build_directives({}, open_day_goals, dirty_tree_verdict)
    judgment_points = wc_brief._build_judgment_points(open_day_goals, dirty_tree_verdict)
    return partition_reportable(judgment_points, directives)


def test_predicate_keeps_the_four_action_class_ids_asked():
    asked, reported = _build_all(_NONEMPTY_OPEN_DAY_GOALS, _DIRTY_TREE)
    assert reported == []
    asked_ids = {p["id"] for p in asked}
    assert _KNOWN_ACTION_CLASS_IDS <= asked_ids
    assert asked_ids == {
        "jp_day_goal_closeout",
        "jp_step2_5_dirty_tree_ambiguous",
        "jp_step3_5_backfill_cap",
        *_KNOWN_ACTION_CLASS_IDS,
    }


def test_known_action_class_points_are_explicitly_marked_not_reportable():
    _, points = (
        wc_brief._build_directives({}, _NONEMPTY_OPEN_DAY_GOALS, _DIRTY_TREE),
        wc_brief._build_judgment_points(_NONEMPTY_OPEN_DAY_GOALS, _DIRTY_TREE),
    )
    by_id = {p["id"]: p for p in points}
    for jid in _KNOWN_ACTION_CLASS_IDS:
        assert by_id[jid]["reportable"] is False, jid


def test_asked_points_include_action_class_points_regardless_of_resolves():
    directives = wc_brief._build_directives({}, _NONEMPTY_OPEN_DAY_GOALS, _DIRTY_TREE)
    directive_ids = {d["id"] for d in directives}
    asked, _ = _build_all(_NONEMPTY_OPEN_DAY_GOALS, _DIRTY_TREE)
    asked_by_id = {p["id"]: p for p in asked}
    for jid in _KNOWN_ACTION_CLASS_IDS:
        point = asked_by_id[jid]
        resolves_ids = {
            rid
            for disposition in point.get("dispositions") or []
            for rid in (disposition.get("resolves") or [])
        }
        assert not (resolves_ids & directive_ids), jid


def test_reported_narration_suffix_empty_when_nothing_reported():
    assert wc_brief._reported_narration_suffix([]) == ""


def _stub_operator_config(monkeypatch) -> None:
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


def test_brief_end_to_end_keeps_action_class_points_in_judgment_points(monkeypatch):
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
    assert _KNOWN_ACTION_CLASS_IDS <= jp_ids
    assert "point(s) gate nothing on this run and are reported, not asked" not in envelope["narration"]
