"""The mutation read deadline and the ceremony performance budget are two
different numbers, and must stay that way.

`CEREMONY_BUDGET_SECS` is a PERFORMANCE bar: an op that exceeds it is a defect
(DR-344). A mutation read deadline is a TRANSPORT question: how long a client
keeps reading for an answer to a request it already sent. They were the same
call (`ipc._timeout_for`), and the consequence was not a slow-op report but an
integrity one -- `warm/client.py` waits `mutation_deadline -
READ_DEADLINE_SECS`, both were 2.0 for every `ceremony.*` op, so the extension
waited zero seconds and every commit on a loaded box came back
`WARM_DISPATCH_INDETERMINATE` and then landed anyway.

Measured 2026-09-20: `ceremony.commit_v2` answers in 354ms of process time
with one git spawn, and takes 5.8-12.5s of wall clock at the stated 50-70
concurrent-session load norm.
"""

from __future__ import annotations

from coordinator_core.ipc import (
    CEREMONY_BUDGET_SECS,
    _timeout_for,
    mutation_read_deadline_for,
)

_CEREMONY_OPS = ("ceremony.commit_v2", "ceremony.close", "ceremony.scoped_git_commit")


def test_the_dispatch_budget_still_clamps_ceremony_ops():
    """The half that must NOT change. Widening this would be a budget
    widening, which is what DR-344 forbids and what this change is not."""
    for op in _CEREMONY_OPS:
        assert _timeout_for(op) == CEREMONY_BUDGET_SECS


def test_the_read_deadline_does_not_inherit_that_clamp():
    """The half that was wrong. A delivered mutation is read for longer than
    the op is budgeted to take, because the question is whether the answer
    arrives, not whether the op was fast."""
    for op in _CEREMONY_OPS:
        assert mutation_read_deadline_for(op) > CEREMONY_BUDGET_SECS


def test_a_ceremony_op_is_not_a_special_case_of_the_read_deadline():
    """Ceremony and non-ceremony mutations resolve the same way here. Before,
    a non-ceremony mutation got the full extension and the ops that actually
    commit got none -- exactly inverted from need."""
    assert mutation_read_deadline_for("ceremony.commit_v2") == mutation_read_deadline_for(
        "fleet.some_unlisted_op"
    )


def test_the_two_resolvers_disagree_and_that_is_the_point():
    """A pin against collapsing one into the other. If these ever return the
    same number for a ceremony op again, the mutation extension is back to
    waiting zero seconds."""
    for op in _CEREMONY_OPS:
        assert mutation_read_deadline_for(op) != _timeout_for(op)
