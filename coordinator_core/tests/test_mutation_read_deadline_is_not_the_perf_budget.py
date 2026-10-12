"""The mutation read deadline and the ceremony process-time budget are two
different numbers, and must stay that way.

The ceremony budget is a process-time bar (state/rulings/2026-10-12-apm-ceremony-budget-process-time.md);
the wall deadline of a ceremony op is the dispatch backstop, which the read
deadline never undercuts. The rationale lives on `ipc.mutation_read_deadline_for`.
Kept separate from `warm/tests/test_client_fallback.py`'s ceremony-extension test,
which pins the CONSEQUENCE one layer up.
"""

from __future__ import annotations

from coordinator_core.ipc import (
    CEREMONY_BUDGET_SECS,
    DISPATCH_TIMEOUT_SECS,
    _timeout_for,
    mutation_read_deadline_for,
)

_CEREMONY_OPS = ("ceremony.commit_v2", "ceremony.close", "ceremony.scoped_git_commit")


def test_the_dispatch_deadline_of_a_ceremony_op_is_the_wall_backstop_not_the_budget():
    for op in _CEREMONY_OPS:
        assert _timeout_for(op) == DISPATCH_TIMEOUT_SECS
        assert _timeout_for(op) > CEREMONY_BUDGET_SECS


def test_the_read_deadline_is_never_below_the_wall_backstop():
    for op in _CEREMONY_OPS:
        assert mutation_read_deadline_for(op) >= _timeout_for(op)
        assert mutation_read_deadline_for(op) > CEREMONY_BUDGET_SECS
