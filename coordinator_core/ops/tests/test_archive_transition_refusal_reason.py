"""Pins that `handoff.archive_transition` surfaces a REFUSED archival move's
own reason instead of asserting a cause it never established.

Finding L1 of `state/audits/2026-08-14-archive-and-commit-restage-src-audit.md`:
a refusal from `archive_and_commit`'s disk/HEAD drift guard (commit 4541069c3)
used to return `moved: False` plus the warning "may already have been moved by
a concurrent session; continuing" — a confident causal claim the op has no
evidence for, with the guard's actual reason discarded from
`failed[0]["reason"]`. Same symptom shape that hid `b51246a1ead1` for a day.

Seams: `archive_and_commit` and `_handoff_has_live_children` are monkeypatched
in the op module's namespace. This file asserts the op's REPORTING of a
refusal, not the guard that produces one (that is
`coordinator_core/ops/fleet/tests/test_archive_and_commit_disk_head_drift.py`)
— so no git spawn is needed or wanted here.

Negative-spec:
  - Does NOT assert exit_code changes on a refused move. exit_code:0 is the
    contract two callers depend on (`handoff_reconcile_close_terminal._handler`
    treats non-zero as "archival failed" and drops its own success envelope;
    `baton_assemble.apply._dispatch_handoff_supersede_predecessor` unlinks the
    freshly-minted successor when the supersede does not stand). This fix is
    reporting only.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from coordinator_core.ops import handoff_archive_transition as _op

_DRIFT_REASON = (
    "disk/HEAD drift: src has uncommitted changes not reflected in HEAD — "
    "refusing move state/handoffs/drifted.md"
)


class ArchiveTransitionRefusalReasonTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.worktree = Path(self._tmp.name).resolve()
        (self.worktree / ".git").mkdir()
        (self.worktree / "state" / "handoffs").mkdir(parents=True)
        self.handoff_path = self.worktree / "state" / "handoffs" / "drifted.md"
        self.handoff_path.write_text(
            "---\n"
            "status: claimed\n"
            "deployment_state: shipped\n"
            "---\n"
            "body\n",
            encoding="utf-8",
        )

        # The live-children guard used to be stubbed here to a safe verdict so
        # the call could reach the move. It was deleted from the op on
        # 2026-08-28 (PM ruling — see the deletion note in
        # handoff_archive_transition), so there is nothing left to stub and
        # these tests reach the move on their own. Nothing about what they
        # actually assert -- refusal-reason plumbing -- has changed.
        self._orig_move = _op.archive_and_commit
        self.addCleanup(self._restore)

        async def _refusing_move(worktree, moves, subject):
            return [], [{"id": moves[0].candidate_id, "reason": _DRIFT_REASON}]

        _op.archive_and_commit = _refusing_move

    def _restore(self):
        _op.archive_and_commit = self._orig_move

    def _run_chain(self) -> dict:
        return asyncio.run(
            _op._handler(
                {"handoff_path": str(self.handoff_path), "mode": "chain"},
                self.worktree / ".git",
            )
        )

    def test_refusal_reason_reaches_the_envelope(self):
        result = self._run_chain()

        self.assertIs(result.get("moved"), False)
        self.assertEqual(result.get("exit_code"), 0, "exit_code is caller contract")

        warnings = result.get("warnings") or []
        self.assertTrue(
            any(_DRIFT_REASON in w for w in warnings),
            f"the mover's own reason must be on the wire; got {warnings!r}",
        )
        self.assertIn(_DRIFT_REASON, result.get("message") or "")
        self.assertEqual(
            [item.get("reason") for item in result.get("failed") or []],
            [_DRIFT_REASON],
            "additive failed[] must carry archive_and_commit's items verbatim",
        )

    def test_no_concurrent_move_cause_is_asserted(self):
        result = self._run_chain()

        surfaced = " ".join(
            [*(result.get("warnings") or []), result.get("message") or ""]
        ).lower()
        for speculation in ("concurrent", "may already", "continuing"):
            self.assertNotIn(
                speculation,
                surfaced,
                "a refusal must not assert a cause the op never established",
            )

    def test_successful_move_carries_no_failed_key(self):
        async def _accepting_move(worktree, moves, subject):
            return [{"id": moves[0].candidate_id, "archived": True}], []

        _op.archive_and_commit = _accepting_move
        result = self._run_chain()

        self.assertIs(result.get("moved"), True)
        self.assertNotIn("failed", result, "failed[] is additive, refusal-path only")


if __name__ == "__main__":
    unittest.main()
