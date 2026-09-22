"""Tests for ``coordinator_core.ops.review_mint.roster.parse_stages``.

Spec: ``docs/plans/2026-08-19-review-mints-its-own-gated-workflow.md`` task C1.
Pure unit tests on inline fixture dicts — no file I/O, no sibling clone.
"""

import pytest

from coordinator_core.ops.review_mint.roster import (
    RosterFragmentError,
    Stage,
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
            "coordinator:review-integrator": "BLOCKED",
            "coordinator:eng-director": "REJECTED",
        },
        "tiers": {
            "lightweight": {
                "stages": [
                    {"agents": ["coordinator:code-reviewer"]},
                    {"agents": ["coordinator:review-integrator"]},
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
                    {"agents": ["coordinator:review-integrator"]},
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
                    {"agents": ["coordinator:review-integrator"]},
                    {"agents": ["coordinator:eng-director"]},
                ]
            },
        },
    }


def test_v3_lightweight_two_non_gated_stages_in_order():
    stages = parse_stages(_v3_fragment(), "lightweight")
    assert stages == [
        Stage(agents=["coordinator:code-reviewer"], gate=False),
        Stage(agents=["coordinator:review-integrator"], gate=False),
    ]


def test_v3_standard_gate_stage_flagged_and_ordered():
    stages = parse_stages(_v3_fragment(), "standard")
    assert [s.gate for s in stages] == [True, False, False]
    assert stages[0].agents == ["coordinator:prior-art-checker"]
    assert stages[1].agents == [
        "coordinator:code-reviewer",
        "coordinator:staff-eng",
    ]
    assert stages[2].agents == ["coordinator:review-integrator"]


def test_v3_full_gate_stage_mixes_blocking_and_non_blocking_agent():
    # docs-checker maps to null (cannot block) but rides the gate stage for
    # ordering alongside prior-art-checker, which can.
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
                "coordinator:review-integrator",
            ]
        },
    }
    stages = parse_stages(fragment, "standard")
    assert stages == [
        Stage(
            agents=[
                "coordinator:code-reviewer",
                "coordinator:review-integrator",
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
    # A pre-v3 (schema_version 2) fragment with a staged tier but no
    # top-level blocking_verdicts map: no agent can ever be shown to block.
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
    # A stray 'parallel' key must not influence composition-relevant output;
    # this parser does not even look at it. Arity alone will decide
    # serial-vs-parallel downstream (C2), never a flag.
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
    """schema_version 4: two stages, each naming the signal bucket it absorbs.

    The named stage already carries two personas, so the `full` tier's cap of 3
    admits exactly one more and the standard tier's cap of 2 admits none —
    which is what makes the same fragment discriminate the two tiers below.
    """
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
    """A signal lands on the stage whose `accepts_signals` names its bucket,
    and nowhere else. A bucket no stage accepts is dropped silently — the
    fragment decides which buckets exist, not the caller."""
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
    """The same signal is admitted on `full` (cap 3) and refused on `standard`
    (cap 2), because the fragment's gated stage already carries two personas.
    A non-persona signal is never capped."""
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
    """`None` is the ordinary call shape for every caller below v4; it must not
    be read as "no signals matched" in some way that alters the stages."""
    assert parse_stages(_v4_fragment(), "full") == parse_stages(
        _v4_fragment(), "full", signals={}
    )
