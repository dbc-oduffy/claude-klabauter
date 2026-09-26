"""The mutation read deadline and the ceremony performance budget are two
different numbers, and must stay that way.

The rationale lives on `ipc.mutation_read_deadline_for`; this module only pins
the two halves of it. Kept separate from `warm/tests/test_client_fallback.py`'s
ceremony-extension test, which pins the CONSEQUENCE one layer up (that the
extension is non-zero) against the real constants -- the bug was that the real
values coincided, so both layers are load-bearing.
"""

from __future__ import annotations

from coordinator_core.ipc import (
    CEREMONY_BUDGET_SECS,
    _timeout_for,
    mutation_read_deadline_for,
)

_CEREMONY_OPS = ("ceremony.commit_v2", "ceremony.close", "ceremony.scoped_git_commit")


def test_the_dispatch_budget_still_clamps_ceremony_ops():
    for op in _CEREMONY_OPS:
        assert _timeout_for(op) == CEREMONY_BUDGET_SECS


def test_the_read_deadline_does_not_inherit_that_clamp():
    for op in _CEREMONY_OPS:
        assert mutation_read_deadline_for(op) > CEREMONY_BUDGET_SECS
