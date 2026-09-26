
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
    assert (
        git_native._check_deliverable_id_precedence(_BODY_EMBEDDED, "dlv-explicit")
        is True
    )


def test_precedence_check_still_conflicts_against_a_real_block_trailer():
    with pytest.raises(git_native.DeliverableIdAssertionConflictError):
        git_native._check_deliverable_id_precedence(_IN_THE_BLOCK, "dlv-explicit")


def test_precedence_check_still_short_circuits_on_an_agreeing_block_trailer():
    assert git_native._check_deliverable_id_precedence(_IN_THE_BLOCK, "dlv-real") is False
