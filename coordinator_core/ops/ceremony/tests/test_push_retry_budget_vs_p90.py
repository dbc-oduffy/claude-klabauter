"""R26 (docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-fyi-rest.md): guards
`PUSH_RETRY_BUDGET_SECS` against the measured worst-case single-leg `git push`
floor DR-401 established, mirroring the sibling
`CADENCE_PUSH_RETRY_BUDGET_SECS` guard the same measurement already backs.

MEASURED FLOOR (DR-401, n=6, this box, documented 50-70-session load norm):
2.07s, 2.85s, 5.64s, 8.54s, 14.54s, 15.31s -- max 15.31s. A budget below that
floor can time out a push that already succeeded server-side, reporting
`unconfirmed`/`failed` for work that landed -- the exact defect R26 fixes for
`PUSH_RETRY_BUDGET_SECS` (already fixed for the cadence sibling by DR-401
itself).
"""

from coordinator_core.ops.ceremony import push as push_mod

#: The measured worst-case single-leg `git push` floor (DR-401), shared with
#: `CADENCE_PUSH_RETRY_BUDGET_SECS`'s own guard -- kept here rather than
#: imported so this test states its own floor independent of that module's
#: docstring wording.
_MEASURED_PUSH_FLOOR_SECS = 15.31


def test_push_retry_budget_clears_measured_p90_floor():
    """`PUSH_RETRY_BUDGET_SECS` must clear the measured worst-case single-leg
    `git push` floor, never sit below it as the pre-R26 12.0s did."""
    assert push_mod.PUSH_RETRY_BUDGET_SECS >= _MEASURED_PUSH_FLOOR_SECS


def test_push_retry_budget_within_dispatch_end_to_end_guard():
    """The ladder's own budget must stay strictly under the un-raisable
    end-to-end dispatch guard it sits inside (`ipc.DISPATCH_TIMEOUT_SECS`) --
    a ceiling, never a target, per this constant's own docstring."""
    from coordinator_core.ipc import DISPATCH_TIMEOUT_SECS

    assert push_mod.PUSH_RETRY_BUDGET_SECS < DISPATCH_TIMEOUT_SECS


def test_push_retry_budget_clears_cadence_sibling_floor_too():
    """Same measured floor as `CADENCE_PUSH_RETRY_BUDGET_SECS` -- this
    ladder's budget must clear it too, though the two constants stay
    independently-tunable literals (this ladder spans multiple retry legs,
    the cadence sibling a single attempt) rather than one shared value."""
    assert push_mod.PUSH_RETRY_BUDGET_SECS >= push_mod.CADENCE_PUSH_RETRY_BUDGET_SECS
