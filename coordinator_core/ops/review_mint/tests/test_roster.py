
import pytest

from coordinator_core.ops.review_mint.roster import (
    ExecuteReview,
    ReviewAgent,
    RosterFragmentError,
    Stage,
    parse_execute_review,
    parse_stages,
)


def _v3_fragment() -> dict:
    return {
        "schema": "review-roster-fragment",
        "schema_version": 3,
        "blocking_verdicts": {
            "coordinator:prior-art-checker": "BLOCKED-SURFACE-TO-PM",
            "coordinator:docs-checker": None,
            "coordinator:code-reviewer": "BLOCKED",
            "coordinator:staff-eng": "REJECTED",
            "coordinator:code-reviewer-weekly": "BLOCKED",
            "coordinator:eng-director": "REJECTED",
        },
        "tiers": {
            "lightweight": {
                "stages": [
                    {"agents": ["coordinator:code-reviewer"]},
                    {"agents": ["coordinator:code-reviewer-weekly"]},
                ]
            },
            "standard": {
                "stages": [
                    {
                        "gate": True,
                        "agents": ["coordinator:prior-art-checker"],
                    },
                    {
                        "agents": [
                            "coordinator:code-reviewer",
                            "coordinator:staff-eng",
                        ]
                    },
                    {"agents": ["coordinator:code-reviewer-weekly"]},
                ]
            },
            "full": {
                "stages": [
                    {
                        "gate": True,
                        "agents": [
                            "coordinator:prior-art-checker",
                            "coordinator:docs-checker",
                        ],
                    },
                    {
                        "agents": [
                            "coordinator:code-reviewer",
                            "coordinator:staff-eng",
                        ]
                    },
                    {"agents": ["coordinator:code-reviewer-weekly"]},
                    {"agents": ["coordinator:eng-director"]},
                ]
            },
        },
    }


def test_v3_lightweight_two_non_gated_stages_in_order():
    stages = parse_stages(_v3_fragment(), "lightweight")
    assert stages == [
        Stage(agents=["coordinator:code-reviewer"], gate=False),
        Stage(agents=["coordinator:code-reviewer-weekly"], gate=False),
    ]


def test_v3_standard_gate_stage_flagged_and_ordered():
    stages = parse_stages(_v3_fragment(), "standard")
    assert [s.gate for s in stages] == [True, False, False]
    assert stages[0].agents == ["coordinator:prior-art-checker"]
    assert stages[1].agents == [
        "coordinator:code-reviewer",
        "coordinator:staff-eng",
    ]
    assert stages[2].agents == ["coordinator:code-reviewer-weekly"]


def test_v3_full_gate_stage_mixes_blocking_and_non_blocking_agent():
    stages = parse_stages(_v3_fragment(), "full")
    gate_stage = stages[0]
    assert gate_stage.gate is True
    assert gate_stage.agents == [
        "coordinator:prior-art-checker",
        "coordinator:docs-checker",
    ]


def test_v1_flat_list_reads_as_single_non_gated_stage():
    fragment = {
        "schema": "review-roster-fragment",
        "tiers": {
            "standard": [
                "coordinator:code-reviewer",
                "coordinator:overengineering-reviewer",
            ]
        },
    }
    stages = parse_stages(fragment, "standard")
    assert stages == [
        Stage(
            agents=[
                "coordinator:code-reviewer",
                "coordinator:overengineering-reviewer",
            ],
            gate=False,
        )
    ]


def test_not_a_mapping_refuses():
    with pytest.raises(RosterFragmentError):
        parse_stages(["not", "a", "dict"], "standard")


def test_missing_tiers_refuses():
    with pytest.raises(RosterFragmentError, match="tiers"):
        parse_stages({"schema": "review-roster-fragment"}, "standard")


def test_unknown_tier_refuses():
    fragment = _v3_fragment()
    with pytest.raises(RosterFragmentError, match="xxl"):
        parse_stages(fragment, "xxl")


def test_empty_flat_list_refuses():
    fragment = {"tiers": {"lightweight": []}}
    with pytest.raises(RosterFragmentError):
        parse_stages(fragment, "lightweight")


def test_empty_stages_list_refuses():
    fragment = {"tiers": {"lightweight": {"stages": []}}}
    with pytest.raises(RosterFragmentError):
        parse_stages(fragment, "lightweight")


def test_stage_with_no_agents_refuses():
    fragment = {"tiers": {"lightweight": {"stages": [{"agents": []}]}}}
    with pytest.raises(RosterFragmentError):
        parse_stages(fragment, "lightweight")


def test_stage_not_a_mapping_refuses():
    fragment = {"tiers": {"lightweight": {"stages": ["not-a-dict"]}}}
    with pytest.raises(RosterFragmentError):
        parse_stages(fragment, "lightweight")


def test_neither_flat_nor_staged_shape_refuses():
    fragment = {"tiers": {"lightweight": {"not_stages": []}}}
    with pytest.raises(RosterFragmentError):
        parse_stages(fragment, "lightweight")


def test_gate_stage_with_no_blocking_agent_refuses():
    fragment = {
        "blocking_verdicts": {"coordinator:docs-checker": None},
        "tiers": {
            "standard": {
                "stages": [
                    {"gate": True, "agents": ["coordinator:docs-checker"]}
                ]
            }
        },
    }
    with pytest.raises(RosterFragmentError, match="no agent that can block"):
        parse_stages(fragment, "standard")


def test_gate_stage_with_no_blocking_verdicts_map_refuses():
    fragment = {
        "schema_version": 2,
        "tiers": {
            "standard": {
                "stages": [
                    {
                        "gate": True,
                        "agents": ["coordinator:prior-art-checker"],
                    }
                ]
            }
        },
    }
    with pytest.raises(RosterFragmentError, match="no agent that can block"):
        parse_stages(fragment, "standard")


def test_parallel_key_is_never_read():
    fragment = {
        "tiers": {
            "lightweight": {
                "stages": [
                    {
                        "parallel": False,
                        "agents": [
                            "coordinator:code-reviewer",
                            "coordinator:staff-eng",
                        ],
                    }
                ]
            }
        }
    }
    stages = parse_stages(fragment, "lightweight")
    assert stages == [
        Stage(
            agents=["coordinator:code-reviewer", "coordinator:staff-eng"],
            gate=False,
        )
    ]


def _v4_fragment() -> dict:
    return {
        "schema": "review-roster-fragment",
        "schema_version": 4,
        "blocking_verdicts": {"coordinator:code-reviewer": "BLOCKED"},
        "tiers": {
            "full": {
                "stages": [
                    {
                        "agents": ["coordinator:premise-checker"],
                        "accepts_signals": "preflight",
                    },
                    {
                        "agents": [
                            "coordinator:code-reviewer",
                            "coordinator:staff-eng",
                            "coordinator:vp-product",
                        ],
                        "gate": True,
                        "accepts_signals": "named",
                    },
                ]
            },
            "standard": {
                "stages": [
                    {
                        "agents": [
                            "coordinator:code-reviewer",
                            "coordinator:staff-eng",
                            "coordinator:vp-product",
                        ],
                        "gate": True,
                        "accepts_signals": "named",
                    }
                ]
            },
        },
    }


def test_accepts_signals_routes_each_bucket_to_its_own_stage():
    stages = parse_stages(
        _v4_fragment(),
        "full",
        signals={
            "preflight": ["coordinator:prior-art-checker"],
            "named": ["coordinator:docs-checker"],
            "unclaimed": ["coordinator:coverage-auditor"],
        },
    )
    assert stages[0].agents == [
        "coordinator:premise-checker",
        "coordinator:prior-art-checker",
    ]
    assert stages[1].agents == [
        "coordinator:code-reviewer",
        "coordinator:staff-eng",
        "coordinator:vp-product",
        "coordinator:docs-checker",
    ]
    assert all(
        "coordinator:coverage-auditor" not in stage.agents for stage in stages
    )


def test_accepts_signals_honours_the_tier_persona_cap():
    signals = {
        "named": ["coordinator:eng-director", "coordinator:docs-checker"],
    }

    full = parse_stages(_v4_fragment(), "full", signals=signals)
    assert full[1].agents[-2:] == [
        "coordinator:eng-director",
        "coordinator:docs-checker",
    ]

    standard = parse_stages(_v4_fragment(), "standard", signals=signals)
    assert "coordinator:eng-director" not in standard[0].agents
    assert standard[0].agents[-1] == "coordinator:docs-checker"


def test_a_signal_already_on_the_stage_is_not_duplicated():
    stages = parse_stages(
        _v4_fragment(),
        "full",
        signals={"named": ["coordinator:staff-eng"]},
    )
    assert stages[1].agents.count("coordinator:staff-eng") == 1


def test_omitted_signals_leave_a_v4_fragment_untouched():
    assert parse_stages(_v4_fragment(), "full") == parse_stages(
        _v4_fragment(), "full", signals={}
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
    with pytest.raises(RosterFragmentError, match="exactly one 'integration' stage"):
        parse_execute_review(fragment)


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


def test_parse_stages_untouched_for_v4_fragment():
    # D6: "A v4 fragment keeps today's parse_stages path" -- parse_stages
    # is unaffected by parse_execute_review's addition.
    assert parse_stages(_v4_fragment(), "full") == parse_stages(
        _v4_fragment(), "full"
    )
