"""The contended-lock wait is a one-directional ratchet.

`locked_write.CONTENDED_LOCK_WAIT_SECS` bounds how long any caller of
`contended_lock_wait_secs()` will sleep on a per-destination publish lock —
`percolate-round.py`, `percolate-mirror.py`, and `publish.py`. This module is the
enforcement: it fails on any edit that raises the ceiling, and on any route that
lets the `COORDINATOR_LOCK_WAIT_SECS` env knob or the `default` parameter resolve
above it.

Why this test exists rather than trusting the constant: the knob was born
deliberately unclamped, with a plausible argument attached — a session that must
land a specific commit in a mirror has decided queueing beats respawning, so let
it wait an hour. The argument is only half right. Queueing does beat respawning,
and that is why the 180s default stays. But an unclamped knob is not a queue
policy, it is an invitation: the EM that hits contention exports a bigger number
and re-runs, and the box carries a process asleep for as long as the number says,
on a machine where 50-70 peers are already queued behind it. A wait that has to be
raised to succeed is a contention defect, and raising the number is what keeps it
unexamined. So the ceiling is pinned here by a second, independent literal, and
lifting the constant without lifting this one fails the suite.

Negative spec -- what this module does NOT assert, deliberately:
  - It does NOT assert any round ACQUIRES within the wait. That is a contention
    property of the box, not a property of this constant.
  - It does NOT forbid resolving BELOW the ceiling. Narrowing is the permitted
    direction: an operator dialling the env knob down for a fast-fail run is a
    supported use, and asserting equality would break it.
  - It does NOT constrain `LOCK_TIMEOUT_SECS`, the single-file RMW timeout. That
    is a separate number with a separate rationale (ten seconds of contention on
    one file means the holder is wedged), and conflating them here would make an
    unrelated RMW retune read as a publish-wait breach.

Load-norm backlink: docs/wiki/machine-load-norm.md and CLAUDE.md § Load norm --
the load is us, and a sleeping process is a process the box is holding.
Mechanism backlink: docs/reference/percolate-lock-contention.md
"""
from __future__ import annotations

import pytest

from coordinator_core import locked_write

#: The independent second copy of the ceiling. Deliberately a literal, not an
#: import of the constant under test -- importing it would make this file agree
#: with any value whatsoever and assert nothing. Lowering it is fine (ratchets
#: lower); raising it requires a PM ruling.
PINNED_CEILING_SECS = 180.0


def test_wait_constant_is_at_or_below_the_pinned_ceiling():
    """The ratchet itself: the constant may be lowered, never raised."""
    assert locked_write.CONTENDED_LOCK_WAIT_SECS <= PINNED_CEILING_SECS, (
        f"contended lock wait raised to {locked_write.CONTENDED_LOCK_WAIT_SECS}s, "
        f"above the pinned {PINNED_CEILING_SECS}s ceiling. This wait ratchets DOWN "
        f"only. A round that needs a longer wait is contending too hard -- shorten "
        f"the hold or run fewer rounds against one dest. Raising this pair requires "
        f"a PM ruling."
    )


def test_default_resolution_is_the_ceiling(monkeypatch):
    monkeypatch.delenv(locked_write.CONTENDED_LOCK_WAIT_ENV, raising=False)
    assert locked_write.contended_lock_wait_secs() <= PINNED_CEILING_SECS


@pytest.mark.parametrize("raw", ["181", "900", "3600", "1e9", "180.0001", "inf", "nan"])
def test_env_knob_cannot_widen_the_wait(monkeypatch, raw):
    """The clamp sits AFTER env resolution -- the escape hatch is the point of it."""
    monkeypatch.setenv(locked_write.CONTENDED_LOCK_WAIT_ENV, raw)
    assert locked_write.contended_lock_wait_secs() <= PINNED_CEILING_SECS


def test_env_knob_may_still_narrow_the_wait(monkeypatch):
    """Narrowing is the permitted direction; a fast-fail run must stay possible."""
    monkeypatch.setenv(locked_write.CONTENDED_LOCK_WAIT_ENV, "5")
    assert locked_write.contended_lock_wait_secs() == pytest.approx(5.0)


def test_a_caller_supplied_default_cannot_widen_the_wait(monkeypatch):
    """The `default` parameter is the second route in, and it is clamped too.

    Closing only the env var would leave the same widening available one import
    away -- a caller passing `default=3600.0` would sleep for an hour with no env
    var set and nothing to notice it.
    """
    monkeypatch.delenv(locked_write.CONTENDED_LOCK_WAIT_ENV, raising=False)
    assert locked_write.contended_lock_wait_secs(default=3600.0) <= PINNED_CEILING_SECS


def test_a_widening_default_and_a_widening_env_knob_together_are_still_clamped(monkeypatch):
    """Defence in depth: both routes open at once still resolve within the ceiling."""
    monkeypatch.setenv(locked_write.CONTENDED_LOCK_WAIT_ENV, "7200")
    assert locked_write.contended_lock_wait_secs(default=3600.0) <= PINNED_CEILING_SECS


def test_a_narrowing_default_still_bounds_a_widening_env_knob(monkeypatch):
    """A caller that asked for a SHORTER wait does not get the ceiling instead."""
    monkeypatch.setenv(locked_write.CONTENDED_LOCK_WAIT_ENV, "900")
    assert locked_write.contended_lock_wait_secs(default=20.0) == pytest.approx(20.0)


def test_the_rmw_timeout_is_untouched_by_this_ratchet():
    """Scoped to the publish wait; the single-file RMW timeout is a separate rule."""
    assert locked_write.LOCK_TIMEOUT_SECS < PINNED_CEILING_SECS
