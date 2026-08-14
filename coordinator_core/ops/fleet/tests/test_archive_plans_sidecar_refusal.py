"""Pins that `fleet.archive_completed_plans` reports a REFUSED sidecar move
instead of letting a partial outcome read as a clean success.

Finding L3 of `state/audits/2026-08-14-archive-and-commit-restage-src-audit.md`:
a sidecar whose git-mv was refused produced a `_LOG.warning` only, while the
primary plan still reported `acted: True` and the envelope derived
`exit_code: 0` — the plan archived, the sidecar left behind in `docs/plans/`,
and nothing on the wire saying so. Reachable because the primary's
`_plan_worktree_dirty` guard is evaluated against the primary path only.

`acted[]`'s frozen `{id, archived}` shape is untouched (see
`_common.build_act_result`'s WIRE-SAFETY note) — the sidecar is reported in
`failed[]` under its own repo-relative id, which is what makes the envelope
read determinate-partial (contract §3.2 exit_code:2).

Seams: `archive_and_commit`, the dirty-tree/claim-liveness guards, and the
post-move backlink gate are monkeypatched in the op module's namespace, so this
file asserts result mapping only — no git spawn, no corpus scan.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from coordinator_core.ops.fleet import archive_plans as _op

_DRIFT_REASON = (
    "disk/HEAD drift: src has uncommitted changes not reflected in HEAD — "
    "refusing move docs/plans/2026-08-01-a-plan.review.md"
)

_PLAN_ID = "docs/plans/2026-08-01-a-plan.md"
_SIDECAR_ID = "docs/plans/2026-08-01-a-plan.review.md"


class SidecarRefusalLegibilityTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.worktree = Path(self._tmp.name).resolve()
        self.common_dir = self.worktree / ".git"
        self.common_dir.mkdir()
        self.plans_dir = self.worktree / "docs" / "plans"
        self.plans_dir.mkdir(parents=True)
        (self.plans_dir / "2026-08-01-a-plan.md").write_text(
            "---\nstatus: implemented\n---\nbody\n", encoding="utf-8"
        )
        (self.plans_dir / "2026-08-01-a-plan.review.md").write_text(
            "review sidecar\n", encoding="utf-8"
        )

        self._orig = {
            "move": _op.archive_and_commit,
            "dirty": _op._plan_worktree_dirty,
            "claim": _op._plan_claim_live,
            "gate": _op._run_backlink_gate,
        }
        self.addCleanup(self._restore)

        async def _not_dirty(worktree_root, rel_path):
            return False

        async def _not_claimed(common_dir, plan_path):
            return False

        _op._plan_worktree_dirty = _not_dirty
        _op._plan_claim_live = _not_claimed
        _op._run_backlink_gate = lambda root: 0

    def _restore(self):
        _op.archive_and_commit = self._orig["move"]
        _op._plan_worktree_dirty = self._orig["dirty"]
        _op._plan_claim_live = self._orig["claim"]
        _op._run_backlink_gate = self._orig["gate"]

    def _install_move(self, refuse_sidecar: bool):
        async def _move(worktree_root, moves, subject):
            acted, failed = [], []
            for move in moves:
                is_sidecar = move.candidate_id.startswith(_op._SIDECAR_PREFIX)
                if is_sidecar and refuse_sidecar:
                    failed.append({"id": move.candidate_id, "reason": _DRIFT_REASON})
                else:
                    acted.append({"id": move.candidate_id, "archived": True})
            return acted, failed

        _op.archive_and_commit = _move

    def _run_act(self) -> dict:
        return asyncio.run(
            _op._handle_act(
                "terminal", self.worktree, self.plans_dir, [_PLAN_ID], self.common_dir
            )
        )

    def test_refused_sidecar_is_reported_not_only_logged(self):
        self._install_move(refuse_sidecar=True)
        result = self._run_act()

        self.assertEqual(
            [item["id"] for item in result["acted"]],
            [_PLAN_ID],
            "the primary plan did archive and still reports acted",
        )
        failed = result["failed"]
        self.assertEqual([item["id"] for item in failed], [_SIDECAR_ID])
        self.assertIn(_DRIFT_REASON, failed[0]["reason"])
        self.assertEqual(
            result["exit_code"], 2, "a partial outcome must not read as clean success"
        )

    def test_clean_batch_still_reports_success_only(self):
        self._install_move(refuse_sidecar=False)
        result = self._run_act()

        self.assertEqual([item["id"] for item in result["acted"]], [_PLAN_ID])
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
