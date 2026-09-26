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
