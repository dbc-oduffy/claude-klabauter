"""The review.prep lock wait is a one-directional ratchet.

`review_prep._LOCK_WAIT_SECS` bounds how long a review.prep loser sleeps before it returns the
winner's stored reply or a busy refusal. It is pinned here by an independent literal at the 500ms
brightline (DR-344), so raising the constant without raising this one fails the suite.

Negative spec -- what this module does NOT assert:
  - It does NOT assert any loser acquires or is served within the wait.
  - It does NOT forbid narrowing the wait; only raising it fails.
"""
from __future__ import annotations

from coordinator_core.ops import review_prep

PINNED_CEILING_SECS = 0.5


def test_lock_wait_is_at_or_below_the_pinned_ceiling():
    assert review_prep._LOCK_WAIT_SECS <= PINNED_CEILING_SECS, (
        f"review.prep lock wait raised to {review_prep._LOCK_WAIT_SECS}s, above the pinned "
        f"{PINNED_CEILING_SECS}s ceiling. The wait ratchets down only; raising it needs a PM ruling."
    )


def test_a_lock_does_not_go_stale_inside_one_wait():
    assert review_prep._LOCK_STALE_SECS > review_prep._LOCK_WAIT_SECS
