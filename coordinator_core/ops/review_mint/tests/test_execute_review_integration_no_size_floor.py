"""compose_execute_review: the Opus integration stage carries no size floor and ends the review."""

import copy

from coordinator_core.ops.review_mint import final_review
from coordinator_core.ops.review_mint.execute_review import OperativeCriterion, compose_execute_review
from coordinator_core.ops.review_mint.final_review import final_review_mandate
from coordinator_core.ops.review_mint.post_exec_tail import ReviewGroup
from coordinator_core.ops.review_mint.roster import parse_execute_review
from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS, _v5_fragment

_STATEMENT = "TARGET-NO-FLOOR"
_GROUPS = [
    ReviewGroup(index=0, rows=("C1",), paths=("a/one.py",), quiescence_rows=("C1",)),
    ReviewGroup(index=1, rows=("C2",), paths=("b/two.py",), quiescence_rows=("C2",)),
]


def _roster():
    fragment = copy.deepcopy(_v5_fragment())
    for stage in fragment["execute_review"]["stages"]:
        if stage["kind"] == "integration":
            stage["agents"] = [
                {
                    "agentType": "coordinator:overengineering-reviewer",
                    "model": "opus",
                    "effort": "low",
                    "applies": "residue",
                    "schema": "review-integration-result",
                }
            ]
        if stage["kind"] == "review-wave":
            stage["agents"][0]["model"] = "sonnet"
    assert not any("min_tshirt" in a for s in fragment["execute_review"]["stages"] for a in s["agents"])
    return parse_execute_review(fragment, signals={"named": ["coordinator:staff-eng"]})


def _compose(**extra):
    kwargs = dict(
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["a/one.py", "b/two.py"],
        prompt_head="BRIEF",
        run_key="k",
        executor_rows=2,
        reverify=True,
        tshirt="S",
        criterion=OperativeCriterion(statement=_STATEMENT),
    )
    kwargs.update(extra)
    return compose_execute_review(_roster(), **kwargs)


def test_small_plan_still_gets_the_opus_integration_pass_before_reverify():
    phases = _compose()
    titles = [t for t, _ in phases]
    assert final_review.FINAL_REVIEW_PHASE == "Review integration"
    assert titles.index("Review integration") < titles.index("Review re-verify")
    block = dict(phases)["Review integration"]
    assert "agentType: 'coordinator:overengineering-reviewer'" in block
    assert "model: 'opus'" in block
    assert block.count(final_review_mandate(_STATEMENT).split("\n")[0]) == 1


def test_integration_is_not_wrapped_in_a_tshirt_gate_at_fire_time_either():
    for kwargs in ({"tshirt": "S"}, {"tshirt": None, "tshirt_js": "_tshirt"}):
        block = dict(_compose(**kwargs))["Review integration"]
        assert "_tshirt" not in block
        assert ".includes(" not in block
        assert "coordinator:overengineering-reviewer" in block


def test_each_review_group_launch_carries_the_sonnet_model():
    block = dict(_compose(review_groups=_GROUPS))["Review groups"]
    assert block.count("model: 'sonnet'") >= len(_GROUPS)
    assert "model: 'opus'" not in block
