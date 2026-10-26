"""review_wave_args reads models and tiers from the DoE plan_review shape; nothing is hard-coded."""

from __future__ import annotations

import copy

import pytest

from coordinator_core.ops.dispatch_emit.review_wave import (
    PARALLEL_REVIEW_ARG,
    REVISE_AGENT_TYPE,
    REVISE_MODEL,
    review_wave_args,
)

ROSTER = {
    "tiers": {
        "XS": {"reviewers": [{"agentType": "coordinator:plan-reviewer", "model": "sonnet"}]},
        "XL": {
            "reviewers": [
                {"agentType": "coordinator:staff-eng", "model": "opus"},
                {"agentType": "coordinator:apm", "model": "sonnet"},
            ]
        },
        "XXL": {"reviewers": [{"agentType": "coordinator:staff-eng", "model": "opus"}]},
    },
    "signal_reviewers": {"ux-flow": {"agentType": "coordinator:staff-ux", "model": "opus"}},
    "lead_round_cap": None,
    "revise": {"step": "merged", "after": "parallel-review-wave"},
}


def test_arg_key():
    assert PARALLEL_REVIEW_ARG == "parallelReview"


def test_emits_models_tiers_revise():
    assert review_wave_args(ROSTER) == {
        "models": {
            "coordinator:plan-reviewer": "sonnet",
            "coordinator:staff-eng": "opus",
            "coordinator:apm": "sonnet",
            "coordinator:staff-ux": "opus",
        },
        "tiers": {
            "coordinator:plan-reviewer": ["XS"],
            "coordinator:staff-eng": ["XL", "XXL"],
            "coordinator:apm": ["XL"],
        },
        "revise": {"agent": REVISE_AGENT_TYPE, "model": REVISE_MODEL},
    }


def test_model_swap_changes_output():
    swapped = copy.deepcopy(ROSTER)
    swapped["tiers"]["XL"]["reviewers"][1]["model"] = "haiku"
    assert review_wave_args(swapped)["models"]["coordinator:apm"] == "haiku"


def test_model_conflict_across_tiers_is_a_hard_error_naming_both():
    bad = copy.deepcopy(ROSTER)
    bad["tiers"]["XXL"]["reviewers"][0]["model"] = "sonnet"
    with pytest.raises(ValueError, match=r"coordinator:staff-eng.*tiers\.XL.*tiers\.XXL"):
        review_wave_args(bad)


def test_model_conflict_with_signal_reviewer_is_a_hard_error():
    bad = copy.deepcopy(ROSTER)
    bad["signal_reviewers"]["ux-flow"] = {"agentType": "coordinator:apm", "model": "opus"}
    with pytest.raises(ValueError, match=r"signal_reviewers\.ux-flow"):
        review_wave_args(bad)


def test_old_agents_shape_is_not_read():
    old = {
        "agents": [{"agentType": "a", "model": "m"}],
        "revise": {"agentType": "r", "model": "m"},
    }
    assert review_wave_args(old) is None


def test_absent_or_unusable_roster_yields_none():
    assert review_wave_args(None) is None
    assert review_wave_args({}) is None
    no_tiers = copy.deepcopy(ROSTER)
    no_tiers["tiers"] = {}
    assert review_wave_args(no_tiers) is None
    no_revise = copy.deepcopy(ROSTER)
    del no_revise["revise"]
    assert review_wave_args(no_revise) is None
    wrong_step = copy.deepcopy(ROSTER)
    wrong_step["revise"]["step"] = "other"
    assert review_wave_args(wrong_step) is None
    bad = copy.deepcopy(ROSTER)
    del bad["tiers"]["XS"]["reviewers"][0]["model"]
    assert review_wave_args(bad) is None
