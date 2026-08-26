"""`git_native._trailer_value` reads git's trailer BLOCK, not any colon-shaped
line in the message.

Same defect as the `prepare-commit-msg` hook's presence check (see
`coordinator/tests/test_prepare_commit_msg_trailer_block_parity.py`), reached
through the two callers that ask for a VALUE rather than presence:

- `_check_deliverable_id_precedence` raised
  `DeliverableIdAssertionConflictError` against a body line no consumer can
  read, blocking a commit over a disagreement that did not exist on the wire;
- `commit_authored_new_file` validated the caller's asserted `deliverable_id`
  against that same body line and accepted a message whose trailer was
  unjoinable -- the failure mode is silent, which is worse.

Spec: cross-repo/inbox/2026-08-26-example-retrieval-repo-em-chunk-trailer-misplacement-
defeats-presence-check.md.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.ceremony import git_native

_BODY_EMBEDDED = (
    "chore: land chunk\n\n"
    "Deliverable-Id: dlv-in-the-body\n\n"
    "Co-Authored-By: A <a@example.com>\n"
)
_IN_THE_BLOCK = "chore: land chunk\n\nbody\n\nDeliverable-Id: dlv-real\n"


def test_a_body_line_reads_as_absent():
    assert git_native._trailer_value(_BODY_EMBEDDED, "Deliverable-Id:") is None


def test_a_block_line_reads_as_its_value():
    assert git_native._trailer_value(_IN_THE_BLOCK, "Deliverable-Id:") == "dlv-real"


def test_precedence_check_does_not_conflict_against_a_body_line():
    """A body-embedded id is not a pre-existing trailer, so the caller's
    explicit value stands and still needs emitting (True)."""
    assert (
        git_native._check_deliverable_id_precedence(_BODY_EMBEDDED, "dlv-explicit")
        is True
    )


def test_precedence_check_still_conflicts_against_a_real_block_trailer():
    with pytest.raises(git_native.DeliverableIdAssertionConflictError):
        git_native._check_deliverable_id_precedence(_IN_THE_BLOCK, "dlv-explicit")


def test_precedence_check_still_short_circuits_on_an_agreeing_block_trailer():
    assert git_native._check_deliverable_id_precedence(_IN_THE_BLOCK, "dlv-real") is False
