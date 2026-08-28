"""A receipt must be stamped with the reviewer's TYPE, never a dispatch label.

Both eligibility checks (`_is_delegate_reviewer`, `_is_review_integrator`)
read `agent_type` AND `subagent_type`, because which one carries the persona
is not fixed across callers. The stamp beneath them read only `agent_type`.
For a NAMED (Agent-teams teammate) dispatch that field holds the teammate's
own name rather than a `coordinator:*` type — the same one-leg/two-leg
asymmetry `block_reviewer_bash_outside_allowlist`'s Divergence 16 fixed in
its `effective_type` selection.

The result was a receipt no reader could ever credit:
`receipt_credit._counting_receipt_stamps` requires the stamped type's bare
form to be a `DELEGATE_REVIEWERS` member, and a teammate name is not one. Of
2899 sidecars measured 2026-08-28 exactly one failed a receipt condition, and
this was it — a real `coordinator:staff-eng` plan review dispatched as
`the Staff Engineer-gate`, silently dropped from coverage.

Negative spec these tests also pin: the fix NEVER widens the vocabulary.
Admitting `the Staff Engineer-gate` would ratify the mislabel and grow a closed set by one
for every label anyone picks.
"""

from __future__ import annotations

import pytest

from coordinator_core.reviewer_vocabulary import DELEGATE_REVIEWERS
from coordinator_core.subagent_sandbox.provision_report import (
    _INTEGRATOR_AGENT_TYPE,
    _is_delegate_reviewer,
    _receipt_agent_type,
)


def test_a_named_dispatch_stamps_the_type_not_the_label():
    """The reported case, end to end: `coordinator:staff-eng` dispatched with
    the name `the Staff Engineer-gate`."""
    agent_type, subagent_type = "patrik-gate", "coordinator:staff-eng"

    assert _is_delegate_reviewer(agent_type, subagent_type), (
        "eligibility already resolved this correctly — only the stamp was wrong"
    )
    assert (
        _receipt_agent_type(agent_type, subagent_type, DELEGATE_REVIEWERS)
        == "coordinator:staff-eng"
    )


def test_the_stamped_type_is_creditable_by_the_reader():
    """The whole point: the stamped value must pass the check
    `receipt_credit._counting_receipt_stamps` applies."""
    stamped = _receipt_agent_type("patrik-gate", "coordinator:staff-eng", DELEGATE_REVIEWERS)

    bare = stamped.rpartition(":")[2] if ":" in stamped else stamped
    assert bare in DELEGATE_REVIEWERS

    dropped = "patrik-gate"
    assert dropped not in DELEGATE_REVIEWERS, (
        "the label must stay OUT of the vocabulary — admitting it would ratify "
        "the mislabel instead of fixing it"
    )


@pytest.mark.parametrize(
    "agent_type, subagent_type, expected",
    [
        # Unnamed dispatch: agent_type already carries the type. Unchanged.
        ("coordinator:code-reviewer", "", "coordinator:code-reviewer"),
        ("coordinator:code-reviewer", "coordinator:staff-eng", "coordinator:code-reviewer"),
        # Named dispatch: only subagent_type carries it.
        ("archive-guard", "coordinator:code-reviewer", "coordinator:code-reviewer"),
        ("", "coordinator:staff-eng", "coordinator:staff-eng"),
    ],
)
def test_prefers_agent_type_when_both_resolve(agent_type, subagent_type, expected):
    assert _receipt_agent_type(agent_type, subagent_type, DELEGATE_REVIEWERS) == expected


def test_never_invents_a_type_when_neither_label_resolves():
    """Bytes reproduced exactly for anything the gates would not have admitted:
    no label resolving means `agent_type` is returned untouched."""
    assert _receipt_agent_type("odd-name", "also-odd", DELEGATE_REVIEWERS) == "odd-name"
    assert _receipt_agent_type("", "", DELEGATE_REVIEWERS) == ""


def test_the_integrator_receipt_has_the_same_resolution():
    """Same asymmetry, same seam — fixed together so the integrator branch does
    not become the surviving instance of the bug."""
    assert (
        _receipt_agent_type(
            "some-label", "coordinator:review-integrator", {_INTEGRATOR_AGENT_TYPE}
        )
        == "coordinator:review-integrator"
    )
