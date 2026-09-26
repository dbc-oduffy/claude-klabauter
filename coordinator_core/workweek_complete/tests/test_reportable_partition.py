
from __future__ import annotations

import pytest

from coordinator_core.workweek_complete.brief import (
    _build_directives,
    _build_judgment_points,
    _reported_narration,
    brief,
)
from coordinator_core.contract.decision_object.judgment import partition_reportable

_EXPECTED_REPORTED_IDS: set[str] = set()

_EXPECTED_ACTION_CLASS_IDS = {
    "jp_step4_triage_dispatch",
    "jp_step7_rule5_already_reviewed_span",
}


def _built_directives_and_points():
    directives = _build_directives()
    points = _build_judgment_points()
    return directives, points


def test_predicate_classification_matches_known_pair():
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
    exit_code, envelope = brief()
    assert exit_code == 0, envelope
    ids = {p["id"] for p in envelope["judgment_points"]}
    assert _EXPECTED_ACTION_CLASS_IDS <= ids


@pytest.mark.real_home
def test_no_point_is_demoted_into_narration_today():
    exit_code, envelope = brief()
    assert exit_code == 0, envelope
    narration = envelope["narration"]
    for point_id in _EXPECTED_ACTION_CLASS_IDS:
        assert point_id not in narration


def test_narration_renderer_still_carries_question_and_rationale():
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
