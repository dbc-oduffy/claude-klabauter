"""C4: `workweek_complete.brief` reports its gate-nothing recommendation-
carrying judgment points into `narration` instead of asking them.

Spec backlink: docs/plans/2026-08-15-judgment-points-that-gate-nothing-stop-being-questions.md,
chunk C4. Consumes C1's `partition_reportable`
(`coordinator_core.contract.decision_object.judgment`) -- this module does
not reimplement the predicate, only wires it.
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

#: The two ids C1's census and this plan's chunk body name as
#: execution-confirmed gate-nothing recommendation-carrying points for this
#: assembler. This is a CHECK on the predicate's result, never an input to
#: it -- see `test_predicate_classification_matches_known_pair`.
_EXPECTED_REPORTED_IDS = {"jp_step4_triage_dispatch", "jp_step7_rule5_already_reviewed_span"}


def _built_directives_and_points():
    directives = _build_directives()
    points = _build_judgment_points()
    return directives, points


def test_predicate_classification_matches_known_pair():
    """Classify by the predicate, not by the known-pair list -- this test
    fails loudly (not silently) if the predicate's result ever diverges from
    the plan's execution-confirmed pair, per C4's "classify by the predicate
    regardless" instruction."""
    directives, points = _built_directives_and_points()
    recommendation_carrying = [p for p in points if p.get("recommendation") is not None]
    _, reported = partition_reportable(recommendation_carrying, directives)
    reported_ids = {p["id"] for p in reported}
    assert reported_ids == _EXPECTED_REPORTED_IDS, (
        f"partition_reportable classified {reported_ids!r}, expected exactly "
        f"{_EXPECTED_REPORTED_IDS!r} -- a predicate/plan divergence is a finding "
        "to report, not to silently accept."
    )


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
def test_reported_points_appear_in_narration_with_question_and_rationale():
    exit_code, envelope = brief()
    assert exit_code == 0, envelope
    narration = envelope["narration"]
    assert "jp_step4_triage_dispatch" in narration
    assert "Dispatch the Step 4 triage/prior-art-scan worker for this week?" in narration
    assert (
        "The weekly triage/prior-art scan is the primary signal for what "
        "this week's summary foregrounds; skip only when there is nothing "
        "new to triage." in narration
    )
    assert "jp_step7_rule5_already_reviewed_span" in narration
    assert "Extend Rule-5's already-reviewed span to include this week's commits?" in narration
    assert (
        "Rule-5's already-reviewed span should extend to cover this week's "
        "new commits unless a reviewer explicitly re-scoped it." in narration
    )


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
