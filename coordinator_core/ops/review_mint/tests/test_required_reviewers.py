"""A reviewer the plan or ask names joins the composed execute-review roster at every tier."""

import copy

import pytest

from coordinator_core.ops.review_mint.execute_review import compose_execute_review
from coordinator_core.ops.review_mint.roster import (
    RosterFragmentError,
    parse_execute_review,
    plan_required_reviewers,
)
from coordinator_core.ops.review_mint.tests.test_execute_review import (
    _STAGE_SCHEMAS,
    _v5_fragment,
)

SID = "game-dev:staff-game-dev"


def _named_entry(fragment):
    wave = next(s for s in fragment["execute_review"]["stages"] if s["kind"] == "review-wave")
    return next(a for a in wave["agents"] if a.get("accepts_signals") == "named")


def _compose(review, tier):
    return compose_execute_review(
        review,
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["a.py"],
        tier=tier,
    )


def _wave_block(phases):
    return next(block for title, block in phases if title == "Review wave")


def test_unnamed_reviewer_is_absent_from_the_lightweight_wave():
    review = parse_execute_review(_v5_fragment())
    assert SID not in _wave_block(_compose(review, "lightweight"))


def test_required_reviewer_joins_the_wave_at_the_lightweight_tier():
    review = parse_execute_review(_v5_fragment(), required_reviewers=[SID])
    assert SID in [a.agent_type for a in review.review_wave]
    assert SID in _wave_block(_compose(review, "lightweight"))


def test_required_reviewer_survives_a_tiered_named_entry():
    fragment = _v5_fragment()
    _named_entry(fragment)["tiers"] = ["full"]
    tiered = parse_execute_review(fragment, signals={"named": ["coordinator:staff-eng"]})
    assert "coordinator:staff-eng" not in _wave_block(_compose(tiered, "lightweight"))

    review = parse_execute_review(
        copy.deepcopy(fragment), signals={"named": ["coordinator:staff-eng"]}, required_reviewers=[SID]
    )
    block = _wave_block(_compose(review, "lightweight"))
    assert SID in block
    assert "coordinator:staff-eng" not in block


def test_required_reviewer_already_signal_selected_is_not_duplicated():
    review = parse_execute_review(
        _v5_fragment(), signals={"named": [SID]}, required_reviewers=[SID, SID]
    )
    assert [a.agent_type for a in review.review_wave].count(SID) == 1


def test_required_reviewer_without_a_named_entry_raises():
    fragment = _v5_fragment()
    wave = next(s for s in fragment["execute_review"]["stages"] if s["kind"] == "review-wave")
    wave["agents"] = [a for a in wave["agents"] if a.get("accepts_signals") != "named"]
    with pytest.raises(RosterFragmentError, match="required_reviewers"):
        parse_execute_review(fragment, required_reviewers=[SID])


@pytest.mark.parametrize("bad", ["staff-game-dev", "Game Dev:x", ["ok:x", 3], "game-dev:staff-game-dev"])
def test_malformed_required_reviewers_raise(bad):
    with pytest.raises(RosterFragmentError):
        parse_execute_review(_v5_fragment(), required_reviewers=bad)


def test_plan_frontmatter_carries_required_reviewers():
    text = f"---\ntitle: t\nrequired_reviewers:\n  - {SID}\n---\n# body\n"
    assert plan_required_reviewers(text) == [SID]
    assert plan_required_reviewers("---\ntitle: t\n---\n") == []
    assert plan_required_reviewers(None) == []
    assert plan_required_reviewers("no frontmatter") == []
