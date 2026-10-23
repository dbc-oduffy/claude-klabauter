"""Final-review contract: the integration phase's spelling and its target mandate clause.

Consumer: the integration block of `compose_execute_review` in `execute_review.py`.
"""

FINAL_REVIEW_PHASE = "Review integration"


def final_review_mandate(criterion_statement: str) -> str:
    """Return the clause that makes the integration reviewer the last reviewer, with edit rights.

    `criterion_statement` is placed verbatim, exactly once. Raises ValueError when blank.
    """
    if not isinstance(criterion_statement, str) or not criterion_statement.strip():
        raise ValueError("final_review_mandate: criterion_statement is empty")
    return (
        "You are the last reviewer. The target is: "
        f"{criterion_statement}\n"
        "Edit any declared path to make the target true. Every edit you make is listed "
        "and its covering tests rerun by `Review re-verify`, so leave no edit you "
        "would not defend. Say in your residue ledger which part of the target each edit serves."
    )
