"""C4: `workweek_complete.brief`'s gate-nothing recommendation-carrying
judgment points, corrected by C1b (docs/plans/2026-08-15-judgment-points-
that-gate-nothing-stop-being-questions.md, premise-finding sidecar).

`jp_step4_triage_dispatch` is action-class (channel 3): answering it makes
the EM dispatch a worker directly, with no directive and no gate. C4
originally demoted it into `narration`; C1b's `reportable` marker corrects
that by explicitly marking it `reportable=False` in `brief.py`, so it stays
`asked`. `jp_step7_rule5_already_reviewed_span` is action-class for the same
reason: `extend_span` widens what Rule-5 treats as already reviewed, no
directive applies that, and its own `reason` is `pm-scoped-tradeoff` -- an
answer that by definition matters. Neither point is demoted, so this module
emits no reported points at all today.

Spec backlink: docs/plans/2026-08-15-judgment-points-that-gate-nothing-stop-being-questions.md,
chunk C4 (this file), corrected by chunk C1b. Consumes C1's
`partition_reportable` (`coordinator_core.contract.decision_object.judgment`)
-- this module does not reimplement the predicate, only wires it.
"""

from __future__ import annotations

import pytest

from coordinator_core.workweek_complete.brief import (
    _build_directives,
    _build_judgment_points,
    _reported_narration,
    brief,
)
from coordinator_core.contract.decision_object.judgment import partition_reportable

#: No point in this module is acknowledgement-class today. Kept as an
#: explicit empty set rather than dropped, so a future `reportable=True`
#: here has an obvious home and this file's assertions stay symmetric.
_EXPECTED_REPORTED_IDS: set[str] = set()

#: Explicitly marked reportable=False (action-class) -- both stay `asked`.
_EXPECTED_ACTION_CLASS_IDS = {
    "jp_step4_triage_dispatch",
    "jp_step7_rule5_already_reviewed_span",
}


def _built_directives_and_points():
    directives = _build_directives()
    points = _build_judgment_points()
    return directives, points


def test_predicate_classification_matches_known_pair():
    """Classify by the predicate, not by the known-pair list -- this test
    fails loudly (not silently) if the predicate's result ever diverges from
    the plan's execution-confirmed classification."""
    directives, points = _built_directives_and_points()
    recommendation_carrying = [p for p in points if p.get("recommendation") is not None]
    _, reported = partition_reportable(recommendation_carrying, directives)
    reported_ids = {p["id"] for p in reported}
    assert reported_ids == _EXPECTED_REPORTED_IDS, (
        f"partition_reportable classified {reported_ids!r}, expected exactly "
        f"{_EXPECTED_REPORTED_IDS!r} -- a predicate/plan divergence is a finding "
        "to report, not to silently accept."
    )


def test_action_class_point_carries_explicit_reportable_false():
    """`jp_step4_triage_dispatch` carries `reportable=False` explicitly --
    not merely an absent/`None` marker -- recording the decision to keep
    asking, per AC2/AC3's three-way ledger."""
    _, points = _built_directives_and_points()
    by_id = {p["id"]: p for p in points}
    assert by_id["jp_step4_triage_dispatch"]["reportable"] is False


@pytest.mark.real_home
def test_reported_points_absent_from_judgment_points():
    """`real_home`: `brief()` resolves the real machine-local registry via
    `resolve_operator_config`, which the suite-root HOME quarantine
    (`coordinator_core/conftest.py`) would otherwise blank out, tripping the
    never-fail-the-ceremony TRANSPORT_FAIL backstop instead of exercising
    the real path -- mirrors this module's own
    `test_brief_envelope_preflight_consumes_manifest_matches_module_constant`."""
    exit_code, envelope = brief()
    assert exit_code == 0, envelope
    ids = {p["id"] for p in envelope["judgment_points"]}
    assert ids.isdisjoint(_EXPECTED_REPORTED_IDS)


@pytest.mark.real_home
def test_action_class_point_stays_asked():
    """`jp_step4_triage_dispatch` must be present in `judgment_points[]`,
    never demoted into narration -- the C1b correction's whole point."""
    exit_code, envelope = brief()
    assert exit_code == 0, envelope
    ids = {p["id"] for p in envelope["judgment_points"]}
    assert _EXPECTED_ACTION_CLASS_IDS <= ids


@pytest.mark.real_home
def test_no_point_is_demoted_into_narration_today():
    """Both of this module's gate-nothing points are action-class, so
    `narration` names neither. Pinned so a future `reportable=True` here is a
    deliberate edit to this expectation rather than a silent demotion."""
    exit_code, envelope = brief()
    assert exit_code == 0, envelope
    narration = envelope["narration"]
    for point_id in _EXPECTED_ACTION_CLASS_IDS:
        assert point_id not in narration


def test_narration_renderer_still_carries_question_and_rationale():
    """The demotion renderer itself stays covered even with nothing demoted --
    otherwise the first future `reportable=True` point ships untested."""
    rendered = _reported_narration(
        [
            {
                "id": "jp-example",
                "question": "Acknowledge the thing?",
                "recommendation": {"disposition": "ack", "rationale": "because so"},
            }
        ]
    )
    assert "jp-example" in rendered
    assert "Acknowledge the thing?" in rendered
    assert "because so" in rendered
    assert _reported_narration([]) == ""


@pytest.mark.real_home
def test_tier3_no_recommendation_points_stay_asked():
    """Tier-3 points (`recommendation=None`) are never fed to the predicate
    (see `brief()`'s comment) and must stay in `judgment_points[]`
    unconditionally, even though several of them also carry no `resolves`
    naming a live directive."""
    exit_code, envelope = brief()
    assert exit_code == 0, envelope
    ids = {p["id"] for p in envelope["judgment_points"]}
    for tier3_id in (
        "jp_step7_5_staff_eng_fire_discretion",
        "jp_step8_5_loe_high_water",
        "jp_step9_editorial_bucketing",
        "jp_step10_semver_judgment",
        "jp_step1c_pm_recollection_match",
        "jp_step9_pm_release_notes_gate",
        "jp_step10_5_gh_release_publish",
    ):
        assert tier3_id in ids


def test_reported_narration_helper_empty_on_no_reported_points():
    assert _reported_narration([]) == ""
