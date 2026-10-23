"""compose_execute_review: the integration call carries the final-review target mandate."""

import shutil
import subprocess

import pytest

from coordinator_core.ops.review_mint import final_review
from coordinator_core.ops.review_mint.execute_review import OperativeCriterion, compose_execute_review
from coordinator_core.ops.review_mint.final_review import final_review_mandate
from coordinator_core.ops.review_mint.post_exec_tail import ReviewGroup
from coordinator_core.ops.review_mint.roster import parse_execute_review
from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS, _v5_fragment

_GROUPS = [
    ReviewGroup(index=0, rows=("C1",), paths=("a/one.py",), quiescence_rows=("C1",)),
    ReviewGroup(index=1, rows=("C2",), paths=("b/two.py",), quiescence_rows=("C2",)),
]
_NASTY = "it's `tick` ${x}\nline two"


def _compose(**extra):
    review = parse_execute_review(_v5_fragment(), signals={"named": ["coordinator:staff-eng"]})
    kwargs = dict(
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["a/one.py", "b/two.py"],
        prompt_head="BRIEF",
        run_key="k",
        executor_rows=2,
        reverify=True,
    )
    kwargs.update(extra)
    return compose_execute_review(review, **kwargs)


def _integration(phases):
    return dict(phases)[final_review.FINAL_REVIEW_PHASE]


def test_phase_title_is_the_final_review_spelling():
    titles = [t for t, _ in _compose()]
    assert final_review.FINAL_REVIEW_PHASE in titles
    assert titles.index(final_review.FINAL_REVIEW_PHASE) < titles.index("Review re-verify")


def test_mandate_present_once_with_criterion_and_before_plan_path():
    block = _integration(_compose(criterion=OperativeCriterion(statement="TARGET-XYZ")))
    mandate_head = final_review_mandate("TARGET-XYZ").split("\n")[0]
    assert block.count(mandate_head) == 1
    assert block.count("TARGET-XYZ") == 1
    assert block.index("apply every outstanding finding") < block.index("TARGET-XYZ") < block.index("plan_path:")


def test_without_criterion_prompt_has_no_mandate_and_is_unchanged():
    assert _compose(criterion=None) == _compose()
    assert "You are the last reviewer" not in _integration(_compose())


def test_with_groups_integration_follows_the_wave_that_awaits_both():
    titles = [t for t, _ in _compose(review_groups=_GROUPS, criterion=OperativeCriterion(statement="T"))]
    assert titles.index("Review wave") < titles.index(final_review.FINAL_REVIEW_PHASE) < titles.index("Review re-verify")


@pytest.mark.spawns_process
def test_free_text_statement_keeps_the_script_parseable():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    block = _integration(_compose(criterion=OperativeCriterion(statement=_NASTY)))
    script = "async function f(agent, phase, _verifyOnly, _degrading){\n" + block +"\n}"
    proc = subprocess.run([node, "--check", "-"], input=script, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
