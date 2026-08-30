"""
Pins the DELEGATE_REVIEWERS / CLOSE_RECEIPT_REVIEWERS split in both directions.

Purpose: guards against a future re-merge of the two sets — either by hand-listing
CLOSE_RECEIPT_REVIEWERS as a literal (reintroducing the drift hazard the module
forbids) or by folding "overengineering-reviewer" back into DELEGATE_REVIEWERS
(silently arming commit credit for a proportionality pass; see
docs/plans/2026-08-30-the-close-time-review-floor-excludes-its-mandatory-reviewer.md
§ Anti-scope).

Negative spec: does not test provision_report's consumption of either set (C2's
surface) and does not test the docstring prose itself.
"""

import pathlib

from coordinator_core import reviewer_vocabulary
from coordinator_core.reviewer_vocabulary import (
    CLOSE_RECEIPT_REVIEWERS,
    DELEGATE_REVIEWERS,
)


def test_close_receipt_reviewers_is_strict_superset_of_delegate_reviewers():
    assert DELEGATE_REVIEWERS < CLOSE_RECEIPT_REVIEWERS


def test_the_difference_is_exactly_overengineering_reviewer():
    assert CLOSE_RECEIPT_REVIEWERS - DELEGATE_REVIEWERS == {"overengineering-reviewer"}


def test_overengineering_reviewer_is_not_a_delegate_reviewer():
    assert "overengineering-reviewer" not in DELEGATE_REVIEWERS


def test_derivation_is_live_not_a_hand_listed_literal():
    """The one test a hand-listed literal cannot pass.

    A frozenset carries no provenance at runtime: `CLOSE_RECEIPT_REVIEWERS`
    spelled as a literal listing all nine names is indistinguishable, by any
    membership or subset assertion, from the derived union -- every such
    assertion is a consequence of `CLOSE - DELEGATE == {"overengineering-
    reviewer"}`, which the tests above already pin. So this one reads the
    SOURCE and asserts the assignment is spelled as a union whose left operand
    is the base set, which is the property that makes the derived set unable
    to drift (kira review, 2026-08-30: the prior body asserted
    `augmented <= (CLOSE | {X})`, trivially true given the superset test, and
    its comment claimed a mutation of DELEGATE_REVIEWERS it never performed).
    """
    import ast

    source = pathlib.Path(reviewer_vocabulary.__file__).read_text(encoding="utf-8")
    assign = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "CLOSE_RECEIPT_REVIEWERS"
            for t in node.targets
        )
    )
    assert isinstance(assign.value, ast.BinOp) and isinstance(assign.value.op, ast.BitOr), (
        "CLOSE_RECEIPT_REVIEWERS must be spelled as a union (`DELEGATE_REVIEWERS | {...}`), "
        "not as a hand-listed literal -- a literal reintroduces the drift hazard "
        "reviewer_vocabulary.py's own docstring forbids, and no runtime assertion can catch it."
    )
    assert isinstance(assign.value.left, ast.Name) and assign.value.left.id == "DELEGATE_REVIEWERS", (
        "the union's left operand must be DELEGATE_REVIEWERS itself, so that editing the base "
        "set carries into the derived one by construction."
    )
