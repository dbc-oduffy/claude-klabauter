"""
coordinator_core.pickup_assemble.tests.test_unification_predicate_default

Purpose: reads the SHIPPED value of `_baton_unification_routing_enabled`
(D-I, docs/plans/2026-08-19-batons-unify-into-one-successor.md § C5) on the
FAST tier.

Why this is its own file rather than a case in
`test_baton_unification.py`: that suite is marked `spawns_process` +
`cadence` as a whole, because its fixtures drive a real git harness. A
one-line default that changes behaviour for every pickup on this box should
not be guarded only at cadence gates — a silent revert would sit green on
every commit-time run in between. This predicate needs no fixture at all, so
the check costs nothing here.

Every other test of this seam monkeypatches the predicate in both
directions, deliberately: they pin the routing's behaviour under each arm.
None of them would notice the shipped literal changing. This one does, and
does nothing else.

Run from the repo root: python -m pytest
coordinator_core/pickup_assemble/tests/test_unification_predicate_default.py -q
"""
from __future__ import annotations

import coordinator_core.pickup_assemble as pa


def test_baton_unification_routing_ships_enabled():
    """Flipped ON at `c09345b56`, signalled to DoE-claude in the same
    breath because their `skills/pickup/SKILL.md` and
    `commands/mise-en-place.md` describe the ON behaviour and landed
    same-session on that signal.

    So a revert is a CROSS-REPO act, not a local one: flipping this literal
    back without a paired signal puts this engine's behaviour back into
    contradiction with two live doctrine files in another repo — the exact
    window the default-off period existed to prevent, reopened from the
    other side. If you are here because this test went red, that is the
    thing to check before changing the assertion.
    """
    assert pa._baton_unification_routing_enabled() is True
