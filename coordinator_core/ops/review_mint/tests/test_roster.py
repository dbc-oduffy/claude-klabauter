
import math

import pytest

from coordinator_core.ops.review_mint import roster
from coordinator_core.ops.review_mint.roster import (
    DELIVERY_NOT_ROSTERED,
    agent_not_run_at_tier,
    EMIT_ROUTE_PLAN,
    EMIT_ROUTE_QUEUE,
    require_emit_route,
    ExecuteReview,
    ReviewAgent,
    RosterFragmentError,
    parse_execute_review,
)


# -- roster-v5 `execute_review` (DoE D1 § Contract, schema_version 5) --------


def _v5_fragment() -> dict:
    """DoE D1's pinned example, verbatim (review-inside-execute-plan.md
    § Contract)."""
    return {
        "schema": "review-roster-fragment",
        "schema_version": 5,
        "blocking_verdicts": {
            "coordinator:code-reviewer": "BLOCKED",
            "coordinator:delivery-verifier": "FAIL",
            "...personas": "REJECTED",
        },
        "execute_review": {
            "brightline": {
                "op": "review_brightline_gate",
                "partition_on": "PARTITION-MANDATORY",
            },
            "stages": [
                {
                    "kind": "prep",
                    "agents": [
                        {
                            "agentType": "coordinator:test-runner",
                            "model": "haiku",
                            "effort": "low",
                            "schema": "review-prep-result",
                        }
                    ],
                },
                {
                    "kind": "review-wave",
                    "agents": [
                        {
                            "agentType": "coordinator:code-reviewer",
                            "model": "opus",
                            "effort": "low",
                            "per": "slice",
                            "applies": "own-slice",
                            "schema": "slice-review-result",
                        },
                        {
                            "agentType": "coordinator:overengineering-reviewer",
                            "model": "opus",
                            "effort": "low",
                            "per": "whole-diff",
                            "applies": "none",
                            "schema": "whole-diff-review-result",
                        },
                        {
                            "accepts_signals": "named",
                            "per": "whole-diff",
                            "applies": "none",
                            "schema": "whole-diff-review-result",
                        },
                        {
                            "agentType": "coordinator:delivery-verifier",
                            "model": "sonnet",
                            "effort": "low",
                            "per": "whole-diff",
                            "applies": "none",
                            "schema": "delivery-verdict",
                        },
                    ],
                },
                {
                    "kind": "integration",
                    "agents": [
                        {
                            "agentType": "coordinator:code-reviewer",
                            "model": "opus",
                            "effort": "low",
                            "applies": "residue",
                            "schema": "review-integration-result",
                        }
                    ],
                },
            ],
        },
    }


def test_parse_execute_review_yields_prep_wave_integration_in_order():
    review = parse_execute_review(_v5_fragment())
    assert isinstance(review, ExecuteReview)
    assert review.prep == ReviewAgent(
        agent_type="coordinator:test-runner",
        model="haiku",
        effort="low",
        per=None,
        applies=None,
        schema="review-prep-result",
    )
    assert [a.agent_type for a in review.review_wave] == [
        "coordinator:code-reviewer",
        "coordinator:overengineering-reviewer",
        "coordinator:delivery-verifier",
    ]
    assert review.integration.agent_type == "coordinator:code-reviewer"
    assert review.integration.schema == "review-integration-result"


def test_parse_execute_review_resolves_accepts_signals_into_the_wave():
    review = parse_execute_review(
        _v5_fragment(), signals={"named": ["coordinator:staff-eng"]}
    )
    resolved = [a for a in review.review_wave if a.accepts_signals == "named"]
    assert len(resolved) == 1
    assert resolved[0].agent_type == "coordinator:staff-eng"
    assert resolved[0].model is None
    assert resolved[0].effort is None
    assert resolved[0].schema == "whole-diff-review-result"


def test_parse_execute_review_unresolved_accepts_signals_contributes_nothing():
    review = parse_execute_review(_v5_fragment())
    assert all(a.accepts_signals != "named" for a in review.review_wave)
    assert len(review.review_wave) == 3


def test_parse_execute_review_two_integration_stages_refuses():
    fragment = _v5_fragment()
    fragment["execute_review"]["stages"].append(
        {
            "kind": "integration",
            "agents": [
                {
                    "agentType": "coordinator:code-reviewer",
                    "model": "opus",
                    "effort": "low",
                    "schema": "review-integration-result",
                }
            ],
        }
    )
    with pytest.raises(RosterFragmentError, match="at most one 'integration' stage"):
        parse_execute_review(fragment)


def test_parse_execute_review_zero_integration_stages_yields_none():
    """2026-09-28 PM order step b': the engine tolerates ZERO integration
    stages -- ExecuteReview.integration is None, prep/review-wave unchanged."""
    fragment = _v5_fragment()
    fragment["execute_review"]["stages"] = [
        s for s in fragment["execute_review"]["stages"] if s["kind"] != "integration"
    ]
    review = parse_execute_review(fragment)
    assert review.integration is None
    assert review.prep.agent_type == "coordinator:test-runner"
    assert [a.agent_type for a in review.review_wave] == [
        "coordinator:code-reviewer",
        "coordinator:overengineering-reviewer",
        "coordinator:delivery-verifier",
    ]


def test_parse_execute_review_two_agent_integration_stage_refuses():
    fragment = _v5_fragment()
    fragment["execute_review"]["stages"][2]["agents"].append(
        {
            "agentType": "coordinator:staff-eng",
            "model": "opus",
            "effort": "low",
            "schema": "review-integration-result",
        }
    )
    with pytest.raises(RosterFragmentError, match="exactly one"):
        parse_execute_review(fragment)


def test_parse_execute_review_multi_agent_prep_stage_refuses():
    fragment = _v5_fragment()
    fragment["execute_review"]["stages"][0]["agents"].append(
        {
            "agentType": "coordinator:staff-eng",
            "model": "opus",
            "effort": "low",
            "schema": "review-prep-result",
        }
    )
    with pytest.raises(RosterFragmentError, match="'prep' stage"):
        parse_execute_review(fragment)


def test_parse_execute_review_missing_execute_review_key_refuses():
    with pytest.raises(RosterFragmentError, match="execute_review"):
        parse_execute_review({"schema": "review-roster-fragment"})


def test_parse_execute_review_agent_type_without_model_refuses():
    fragment = _v5_fragment()
    del fragment["execute_review"]["stages"][0]["agents"][0]["model"]
    with pytest.raises(RosterFragmentError, match="no 'model'"):
        parse_execute_review(fragment)


def test_parse_execute_review_agent_type_without_effort_refuses():
    fragment = _v5_fragment()
    del fragment["execute_review"]["stages"][0]["agents"][0]["effort"]
    with pytest.raises(RosterFragmentError, match="invalid 'effort'"):
        parse_execute_review(fragment)


def test_parse_execute_review_accepts_signals_entry_with_model_refuses():
    fragment = _v5_fragment()
    fragment["execute_review"]["stages"][1]["agents"][2]["model"] = "opus"
    with pytest.raises(RosterFragmentError, match="neither 'model' nor 'effort'"):
        parse_execute_review(fragment)


def test_parse_execute_review_unknown_kind_refuses():
    fragment = _v5_fragment()
    fragment["execute_review"]["stages"][0]["kind"] = "mystery"
    with pytest.raises(RosterFragmentError, match="unknown kind"):
        parse_execute_review(fragment)


def test_parse_execute_review_missing_schema_refuses():
    fragment = _v5_fragment()
    del fragment["execute_review"]["stages"][0]["agents"][0]["schema"]
    with pytest.raises(RosterFragmentError, match="'schema'"):
        parse_execute_review(fragment)


_JUDGE_AGENT = {"agentType": "coordinator:criterion-judge", "model": "opus", "effort": "low", "schema": "judge"}


def test_parse_execute_review_resolves_an_optional_judge_stage():
    fragment = _v5_fragment()
    assert parse_execute_review(fragment).judge is None
    fragment["execute_review"]["stages"].append({"kind": "judge", "agents": [_JUDGE_AGENT]})
    assert parse_execute_review(fragment).judge.agent_type == "coordinator:criterion-judge"


def test_parse_execute_review_two_judges_refuses():
    fragment = _v5_fragment()
    fragment["execute_review"]["stages"].append({"kind": "judge", "agents": [_JUDGE_AGENT, _JUDGE_AGENT]})
    with pytest.raises(RosterFragmentError, match="at most one 'judge' stage"):
        parse_execute_review(fragment)


def test_agent_tiers_parse_and_default_to_none():
    fragment = _v5_fragment()
    fragment["execute_review"]["stages"][1]["agents"][3]["tiers"] = ["standard", "full"]
    review = parse_execute_review(fragment, signals={"named": ["coordinator:staff-eng"]})
    by_type = {a.agent_type: a for a in review.review_wave}
    assert by_type["coordinator:delivery-verifier"].tiers == ("standard", "full")
    assert by_type["coordinator:code-reviewer"].tiers is None


def test_accepts_signals_agent_carries_tiers_to_every_resolved_type():
    fragment = _v5_fragment()
    fragment["execute_review"]["stages"][1]["agents"][2]["tiers"] = ["full"]
    review = parse_execute_review(fragment, signals={"named": ["coordinator:staff-eng"]})
    named = [a for a in review.review_wave if a.accepts_signals == "named"]
    assert [a.tiers for a in named] == [("full",)]


@pytest.mark.parametrize("bad", [["huge"], [], "full", ["full", 3]])
def test_unknown_or_malformed_tiers_refuse(bad):
    fragment = _v5_fragment()
    fragment["execute_review"]["stages"][1]["agents"][0]["tiers"] = bad
    with pytest.raises(RosterFragmentError, match="tiers"):
        parse_execute_review(fragment)


def _live_fragment() -> dict:
    """DoE's final shape: ratio on code-reviewer, min_tshirt on Kira, no delivery-verifier."""
    fragment = _v5_fragment()
    wave = fragment["execute_review"]["stages"][1]["agents"]
    wave[0]["executor_rows_per_reviewer"] = {"min": 3, "max": 4}
    wave[1]["min_tshirt"] = "L"
    del wave[3]
    del fragment["blocking_verdicts"]["coordinator:delivery-verifier"]
    return fragment


def test_live_shaped_fragment_parses_both_fields():
    review = parse_execute_review(_live_fragment())
    by_type = {a.agent_type: a for a in review.review_wave}
    assert by_type["coordinator:code-reviewer"].executor_rows_per_reviewer == (3, 4)
    assert by_type["coordinator:code-reviewer"].min_tshirt is None
    assert by_type["coordinator:overengineering-reviewer"].min_tshirt == "L"
    assert by_type["coordinator:overengineering-reviewer"].executor_rows_per_reviewer is None
    assert "coordinator:delivery-verifier" not in by_type


def test_signal_resolved_entry_carries_both_fields():
    fragment = _live_fragment()
    named = fragment["execute_review"]["stages"][1]["agents"][2]
    named["min_tshirt"] = "XL"
    review = parse_execute_review(fragment, signals={"named": ["coordinator:staff-eng"]})
    agent = [a for a in review.review_wave if a.accepts_signals == "named"][0]
    assert agent.min_tshirt == "XL"


@pytest.mark.parametrize(
    "ratio",
    [
        {"min": 5, "max": 4},
        {"min": 0, "max": 4},
        {"min": 3, "max": 0},
        {"min": "3", "max": 4},
        {"min": 3.0, "max": 4},
        {"min": True, "max": 4},
        {"min": 3},
        {"min": 3, "max": 4, "extra": 1},
        [3, 4],
        "3-4",
    ],
)
def test_malformed_ratio_refuses(ratio):
    fragment = _live_fragment()
    fragment["execute_review"]["stages"][1]["agents"][0]["executor_rows_per_reviewer"] = ratio
    with pytest.raises(RosterFragmentError, match="executor_rows_per_reviewer"):
        parse_execute_review(fragment)


def test_ratio_on_whole_diff_entry_refuses():
    fragment = _live_fragment()
    fragment["execute_review"]["stages"][1]["agents"][1]["executor_rows_per_reviewer"] = {"min": 3, "max": 4}
    with pytest.raises(RosterFragmentError, match="per"):
        parse_execute_review(fragment)


@pytest.mark.parametrize("bad", ["XXXL", "l", 3, ""])
def test_unknown_min_tshirt_refuses(bad):
    fragment = _live_fragment()
    fragment["execute_review"]["stages"][1]["agents"][1]["min_tshirt"] = bad
    with pytest.raises(RosterFragmentError, match="min_tshirt"):
        parse_execute_review(fragment)


def test_tshirt_order_mirrors_sizing_assemble():
    from coordinator_core import sizing_assemble

    assert list(roster._TSHIRT_ORDER) == sizing_assemble.TSHIRT_ORDER


def _agent(**kw) -> ReviewAgent:
    return ReviewAgent(
        agent_type="coordinator:code-reviewer", model="sonnet", effort="low",
        per="slice", applies="own-slice", schema="s", **kw,
    )


@pytest.mark.parametrize("rows", range(1, 13))
@pytest.mark.parametrize("slices", range(1, 6))
def test_reviewer_count_is_ceil_rows_over_max_floored_by_min_and_capped_by_slices(rows, slices):
    agent = _agent(executor_rows_per_reviewer=(3, 4))
    assert agent.reviewer_count(rows, slices) == max(1, min(slices, math.ceil(rows / 4), rows // 3))


def test_reviewer_count_without_ratio_is_per_slice():
    assert _agent().reviewer_count(12, 5) == 5


@pytest.mark.parametrize(
    "tshirt,expected",
    [("XS", False), ("S", False), ("M", False), ("L", True), ("XL", True), ("XXL", True), (None, False), ("??", False)],
)
def test_admits_tshirt_floor_l(tshirt, expected):
    assert _agent(min_tshirt="L").admits_tshirt(tshirt) is expected


def test_admits_tshirt_without_floor_admits_all():
    assert _agent().admits_tshirt("XS") is True


def test_agent_not_run_at_tier_true_for_unrostered_type():
    review = parse_execute_review(_live_fragment())
    assert agent_not_run_at_tier(review, "coordinator:delivery-verifier", "full") is True
    assert agent_not_run_at_tier(review, "coordinator:code-reviewer", "full") is False


def test_agent_not_run_at_tier_true_when_tiers_exclude():
    fragment = _live_fragment()
    fragment["execute_review"]["stages"][1]["agents"][1]["tiers"] = ["full"]
    review = parse_execute_review(fragment)
    assert agent_not_run_at_tier(review, "coordinator:overengineering-reviewer", "lightweight") is True
    assert agent_not_run_at_tier(review, "coordinator:overengineering-reviewer", "full") is False


def test_delivery_not_rostered_names_the_verifier():
    assert "coordinator:delivery-verifier" in DELIVERY_NOT_ROSTERED


def test_require_emit_route_listed_passes():
    require_emit_route({"execute_review": {"required_for_emit": [EMIT_ROUTE_PLAN]}}, EMIT_ROUTE_PLAN)


def test_require_emit_route_missing_route_raises():
    with pytest.raises(RosterFragmentError, match="queue"):
        require_emit_route({"execute_review": {"required_for_emit": ["plan"]}}, EMIT_ROUTE_QUEUE)


@pytest.mark.parametrize("fragment", [{}, {"execute_review": {}}, {"execute_review": {"required_for_emit": "plan"}}])
def test_require_emit_route_absent_or_non_list_raises(fragment):
    with pytest.raises(RosterFragmentError):
        require_emit_route(fragment, "plan")
