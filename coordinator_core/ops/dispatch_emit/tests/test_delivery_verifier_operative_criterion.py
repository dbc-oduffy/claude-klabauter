"""An amended sizing criterion supersedes the plan body's exit-criteria list for the delivery verifier."""

from coordinator_core.ops.review_mint.execute_review import (
    OperativeCriterion,
    delivery_supersession_clause,
)


def test_clause_names_operative_statement_and_supersession():
    clause = delivery_supersession_clause(OperativeCriterion("clause A; clause B", "old"))
    assert "SUPERSEDED" in clause and "clause A; clause B" in clause


def test_no_clause_without_amendment():
    assert delivery_supersession_clause(None) == ""
    assert delivery_supersession_clause(OperativeCriterion("same")) == ""
