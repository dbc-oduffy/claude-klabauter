
import pytest

from coordinator_core.ops.review_mint.roster import (
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
