"""Every code-reviewer prompt (whole-diff, group, slice) tells the reviewer to run a new or changed
refusing gate against its real corpus; other agents' prompts do not carry it."""

from __future__ import annotations

from coordinator_core.ops.review_mint.execute_review import (
    NEW_GATE_CORPUS_PROBE_CLAUSE,
    OperativeCriterion,
    compose_execute_review,
)
from coordinator_core.ops.review_mint.post_exec_tail import ReviewGroup
from coordinator_core.ops.review_mint.roster import parse_execute_review
from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS, _v5_fragment

_GROUPS = [ReviewGroup(index=0, rows=("C1",), paths=("a/one.py",), quiescence_rows=("C1",))]


def _wave(**extra) -> str:
    """All composed phase blocks joined: group reviews live outside the "Review wave" block."""
    review = parse_execute_review(_v5_fragment(), signals={"named": ["coordinator:staff-eng"]})
    kwargs = dict(
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["a/one.py"],
        run_key="k",
        executor_rows=1,
    )
    kwargs.update(extra)
    return "\n".join(block for _, block in compose_execute_review(review, **kwargs))


def _count(block: str) -> int:
    return block.count("New-gate corpus probe:")


def test_clause_names_running_the_gate_on_the_real_corpus_and_reporting_refusals():
    assert "real corpus" in NEW_GATE_CORPUS_PROBE_CLAUSE
    assert "false refusal" in NEW_GATE_CORPUS_PROBE_CLAUSE
    assert "gate corpus probe: not run" in NEW_GATE_CORPUS_PROBE_CLAUSE


def test_clause_rides_every_site_that_carries_the_brief_conformance_clause():
    for extra in ({}, {"review_groups": _GROUPS}):
        block = _wave(criterion=OperativeCriterion("s"), **extra)
        assert _count(block) > 0
        assert _count(block) == block.count("Brief conformance:") + block.count("delivery-verifier reviewer")


def test_clause_rides_the_reverify_phase_prompt():
    review = parse_execute_review(_v5_fragment(), signals={"named": ["coordinator:staff-eng"]})
    phases = compose_execute_review(
        review,
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["a/one.py"],
        run_key="k",
        executor_rows=1,
        criterion=OperativeCriterion("s"),
        reverify=True,
    )
    reverify = [block for name, block in phases if name == "Review re-verify"]
    assert reverify
    assert all(_count(block) == 1 for block in reverify)
    assert any(_count(block) > 0 for name, block in phases if name != "Review re-verify")


def test_clause_is_absent_from_non_code_reviewer_prompts():
    block = _wave(criterion=OperativeCriterion("s"))
    assert "staff-eng" in block
    assert _count(block) == block.count("Brief conformance:") + block.count("delivery-verifier reviewer")


def test_clause_rides_the_in_wave_delivery_verifier_prompt():
    review = parse_execute_review(_v5_fragment(), signals={"named": ["coordinator:staff-eng"]})
    phases = compose_execute_review(
        review,
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["a/one.py"],
        run_key="k",
        executor_rows=1,
        criterion=OperativeCriterion("s"),
    )
    assert any("delivery-verifier reviewer" in b and _count(b) > 0 for _, b in phases)


def test_clause_rides_the_delivery_reverify_prompts():
    from coordinator_core.ops.dispatch_emit import reverify_delivery as rd

    claims = [{"claim": "adds the frobnicator", "anchor": "no frob.py in diff"}]
    for rerun_tests in (True, False):
        script = rd.compose_reverify_script(
            fragment=_v5_fragment(), stage_schemas=_STAGE_SCHEMAS, plan_path="p.md",
            run_record_rel="r.md", plan_id="pln-1", run_base_sha="a" * 40, head_sha="b" * 40,
            claims=claims, rerun_tests=rerun_tests, host_degraded=False,
        )
        assert _count(script) >= 1
