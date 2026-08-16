"""coordinator_core.workday_complete.tests.test_reportable_partition — C3,
corrected by C1b (docs/plans/2026-08-15-judgment-points-that-gate-nothing-
stop-being-questions.md, premise-finding sidecar): `workday-complete`'s
`_build_judgment_points` / `_build_day_goal_closeout_judgment_point` output,
routed through C1's `partition_reportable`.

The four Sonnet-dispatch points (`jp_step4b_analyst_dispatch`,
`jp_step4c_observer_dispatch`, `jp_step4_5_clustering_dispatch`,
`jp_step4e_health_ledger_new_rows`) are action-class, not acknowledgement-
class: answering one makes the EM dispatch a worker or write a
health-ledger row directly, with no directive and no gate (premise-finding
sidecar's channel 3). C3 originally demoted them; C1b's `reportable`
marker corrects that by explicitly marking all four `reportable=False` in
`brief.py`, so they stay `asked` -- this test now pins THAT, not the
opposite.

Classifies by the predicate, never by a hard-coded id list -- the four
known-action-class ids are asserted as a CHECK on the predicate's live
output, not fed into it.

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

#: Action-class (reportable=False, explicitly decided) -- gate-nothing on
#: the directive axis, but must stay `asked`, never demoted.
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
    """With every conditional point ALSO present (nonempty open day goals,
    ambiguous dirty tree), the predicate's `reported` set must be EMPTY --
    every gate-nothing point this assembler builds is either genuinely
    gating something or explicitly marked action-class (`reportable=False`),
    never demoted."""
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
    """Each of the four dispatch points carries `reportable=False`
    explicitly -- not merely an absent/`None` marker -- recording that
    keeping them asked was a deliberate call, per AC2/AC3's three-way
    ledger."""
    _, points = (
        wc_brief._build_directives({}, _NONEMPTY_OPEN_DAY_GOALS, _DIRTY_TREE),
        wc_brief._build_judgment_points(_NONEMPTY_OPEN_DAY_GOALS, _DIRTY_TREE),
    )
    by_id = {p["id"]: p for p in points}
    for jid in _KNOWN_ACTION_CLASS_IDS:
        assert by_id[jid]["reportable"] is False, jid


def test_asked_points_include_action_class_points_regardless_of_resolves():
    """The four action-class points gate no directive present on this
    envelope (their `resolves` are empty/non-matching) yet must still be
    `asked` -- the shape of the C1b correction, not a restatement of the id
    list."""
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


def test_brief_end_to_end_keeps_action_class_points_in_judgment_points(monkeypatch):
    """`brief()`'s own emitted envelope must reflect the same corrected
    partition -- the four action-class ids present in `judgment_points[]`,
    never silently demoted into `narration`."""
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
