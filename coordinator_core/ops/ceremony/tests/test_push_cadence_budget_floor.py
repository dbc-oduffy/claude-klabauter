"""
coordinator_core.ops.ceremony.tests.test_push_cadence_budget_floor

Purpose: guard the RELATIONSHIP DR-401 (2026-09-01) establishes --
`CADENCE_PUSH_RETRY_BUDGET_SECS` must never sit below the measured no-op
`git push` floor it budgets against -- not a pin on either number.

Negative spec: this is deliberately NOT `assert CADENCE_PUSH_RETRY_BUDGET_SECS
== 16.0`. A future re-measurement that moves `MEASURED_NOOP_PUSH_FLOOR_SECS`
(or a re-justified budget bump) must not need this test edited -- only a
regression that puts the budget BELOW the floor should ever fail it. The
C5 defect this guards against: a budget sized from a proxy measurement
(`git ls-remote`/`git push --dry-run`) that never opens the same network
round trip as the operation it stands in for, silently below the real cost.
"""

from __future__ import annotations

from coordinator_core.ops.ceremony import push as push_mod


def test_cadence_push_budget_is_at_least_the_measured_noop_push_floor():
    """DR-401's guard: the cadence ladder's own retry budget must clear the
    measured floor of a genuine no-op `git push` -- a budget below that
    floor can never succeed even with zero payload (the live defect this
    pins against: C5's 6.0s sat under a 2.07-15.31s measured floor)."""
    assert (
        push_mod.CADENCE_PUSH_RETRY_BUDGET_SECS
        >= push_mod.MEASURED_NOOP_PUSH_FLOOR_SECS
    )


def test_relationship_discriminates_red_at_the_superseded_c5_value():
    """Proves the guard above is a real discriminator, not a tautology --
    evaluated directly against the superseded C5 constant (6.0) rather than
    mutating module state, so this stays read-only against a shared-tree
    module. C5's own value must fail this relationship; DR-401's value
    must pass it (covered by the test above)."""
    superseded_c5_budget_secs = 6.0
    assert superseded_c5_budget_secs < push_mod.MEASURED_NOOP_PUSH_FLOOR_SECS
