"""Promoted falsifier -- the content clause of the group-em plan's prime exit criterion.

Graduated from `_falsifier_stopped_peer_reaches_gem.py`, which was authored and
baselined BEFORE the spine was written and read False on both legs at
`2a13ff10b5`. That is the whole value of promoting it: this test arrives with its
red state already demonstrated against a recorded baseline, which is more than
most tests can say.

Plan: docs/plans/2026-08-31-the-group-em-tick-carries-standing-obligations.md

WHAT THIS COVERS AND WHAT IT DOES NOT. The criterion has two clauses of different
falsifiability classes and only one is testable here:

  CONTENT (this file): the record names what a peer owes, distinguishes that from
  "no ledger exists", and a tick that sends nothing records which obligation it
  declined and why.

  INITIATION (not testable, do not add a leg for it): "reaches the Group EM
  without the Group EM having taken any action to look". Any in-repo test that
  exercises the delivery path IS the look, so "nobody asked" is a negative
  existential over an out-of-process actor. It is discharged by recorded
  observation -- see `prime_exit_criterion.acceptance_by_observation` in the plan,
  and `falsifier.ADJUDICATION_2026_08_31` for why no probe can exist.

Do not "complete" this file by adding an initiation test. Three falsifiers were
written that way before the split, each encoding a delivery mechanism the design
does not use.
"""
from __future__ import annotations

import pathlib
import tempfile

from coordinator_core.group_em import obligations, send_pass


def test_obligations_are_named_not_merely_counted() -> None:
    """`for_peer` returns the rows behind the count, and absence stays distinct.

    `undischarged_obligations` already gave a count and already separated None
    from 0 before this plan; asserting that alone would be a regression test, not
    a bar. What nothing exposed was WHICH obligations, which is what a wake needs.
    """
    with tempfile.TemporaryDirectory() as tmp:
        assert obligations.for_peer(tmp, "sess-no-ledger-0000000000000000") is None, (
            "a peer with no ledger must read None -- 'no ledger exists' is not "
            "the same claim as 'nothing owed', and collapsing them re-creates "
            "the original bug inverted"
        )

        sid = "sess-has-ledger-000000000000000"
        # Review: overengineering-reviewer (finding #2, minor, accepted) --
        # `send_pass` no longer aliases `subagent_share`'s functions/constants
        # under a private name; this test named `subagent_share.share_dir`/
        # `LEDGER_FILENAME` directly.
        from coordinator_core.session import subagent_share

        share_dir = pathlib.Path(subagent_share.share_dir(tmp, sid))
        share_dir.mkdir(parents=True, exist_ok=True)
        (share_dir / subagent_share.LEDGER_FILENAME).write_text(
            '{"obligation_id": "ob-1", "seam": "review", '
            '"next_action": "ask what it needs", "discharged_at": null, "fired": false}\n'
            '{"obligation_id": "ob-2", "seam": "merge", '
            '"next_action": "reconcile the branch", "discharged_at": null, "fired": false}\n',
            encoding="utf-8",
        )
        rows = obligations.for_peer(tmp, sid)

    assert isinstance(rows, list) and len(rows) == 2
    assert all(isinstance(row, dict) and row.get("next_action") for row in rows), (
        "rows must carry the named next action, not just a count"
    )


def test_empty_tick_records_which_obligation_it_declined_and_why() -> None:
    """A tick that sends nothing cannot close on an empty result.

    Four empty fields are indistinguishable from a tick that never looked, which
    is the failure the whole mechanism exists to end.
    """
    with tempfile.TemporaryDirectory() as tmp:
        digest = send_pass.build_send_digest(
            tmp, roster=[], caller_session_id="sess-caller-0000000000000000"
        )

    declined = digest.get("declined")
    assert declined, "an empty-roster tick closed with no declination"
    assert all(
        isinstance(row, dict) and row.get("obligation") and row.get("reason")
        for row in declined
    ), "every declination names an obligation and a reason"
