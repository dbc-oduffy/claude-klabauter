"""The code-reviewer prompts at all three sites tell the reviewer a gap-titled row is a gap to close;
other agents' prompts do not carry the sentence."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_contract import GAP_TITLE_PREFIX
from coordinator_core.ops.review_mint.execute_review import (
    _BRIEF_CONFORMANCE_CLAUSE,
    OperativeCriterion,
    compose_execute_review,
)
from coordinator_core.ops.review_mint.post_exec_tail import ReviewGroup
from coordinator_core.ops.review_mint.roster import parse_execute_review
from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS, _v5_fragment

_GROUPS = [ReviewGroup(index=0, rows=("C1",), paths=("a/one.py",), quiescence_rows=("C1",))]
_SENTENCE = _BRIEF_CONFORMANCE_CLAUSE[
    _BRIEF_CONFORMANCE_CLAUSE.index("names a gap to close") : _BRIEF_CONFORMANCE_CLAUSE.index("gap's")
]


def _blocks(**extra):
    review = parse_execute_review(_v5_fragment(), signals={"named": ["coordinator:staff-eng"]})
    kwargs = dict(
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["a/one.py"],
        run_key="k",
        executor_rows=1,
        criterion=OperativeCriterion("s"),
    )
    kwargs.update(extra)
    return [block for _, block in compose_execute_review(review, **kwargs)]


def test_clause_carries_the_gap_sentence_and_prefix():
    assert GAP_TITLE_PREFIX in _BRIEF_CONFORMANCE_CLAUSE
    assert "asserts the gap's state is `unmet`" in _BRIEF_CONFORMANCE_CLAUSE


def test_sentence_rides_exactly_where_the_brief_conformance_clause_rides():
    for extra in ({}, {"review_groups": _GROUPS}):
        joined = "\n".join(_blocks(**extra))
        assert joined.count(_SENTENCE) > 0
        assert joined.count(_SENTENCE) == joined.count("Brief conformance:")
        assert joined.count(GAP_TITLE_PREFIX) >= joined.count(_SENTENCE)


def test_sentence_is_absent_from_non_code_reviewer_prompts():
    for block in _blocks():
        if "Brief conformance:" not in block:
            assert _SENTENCE not in block
