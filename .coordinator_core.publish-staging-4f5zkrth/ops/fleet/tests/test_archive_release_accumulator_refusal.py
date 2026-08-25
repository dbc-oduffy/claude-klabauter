"""Pins that `fleet.archive_release_accumulator` puts a refused git-mv's reason
on the wire, distinguishably from every other `archived: False` outcome.

Finding L2 of `state/audits/2026-08-14-archive-and-commit-restage-src-audit.md`:
a refusal returned `{archived: False, dest: None, already_archived: False}` with
the reason reaching `_LOG.error` alone — the same three booleans a setup error
returns, so no caller could tell a refused move from a bad param or from an
absent accumulator.

Seam: `archive_and_commit` is monkeypatched in the op module's namespace — this
file asserts the op's REPORTING of a refusal, not the guard that produces one
(`test_archive_and_commit_disk_head_drift.py` in this directory owns that), so
no git spawn is needed here.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from coordinator_core.ops.fleet import archive_release_accumulator as _op

_DRIFT_REASON = (
    "disk/HEAD drift: src has uncommitted changes not reflected in HEAD — "
    "refusing move state/week-changelog/2026-08-14-pending-release.md"
)


class AccumulatorRefusalLegibilityTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.worktree = Path(self._tmp.name).resolve()
        (self.worktree / ".git").mkdir()
        self.changelog_dir = self.worktree / "state" / "week-changelog"
        self.changelog_dir.mkdir(parents=True)

        self._orig_move = _op.archive_and_commit
        self.addCleanup(self._restore)

    def _restore(self):
        _op.archive_and_commit = self._orig_move

    def _seed_accumulator(self) -> Path:
        path = self.changelog_dir / "2026-08-14-pending-release.md"
        path.write_text("# pending release\n", encoding="utf-8")
        return path

    def _run_act(self) -> dict:
        return asyncio.run(
            _op._handler({"tag": "v1.2.3", "dry_run": False}, self.worktree / ".git")
        )

    def test_refusal_carries_the_reason_and_is_not_absence(self):
        self._seed_accumulator()

        async def _refusing_move(worktree, moves, subject):
            return [], [{"id": moves[0].candidate_id, "reason": _DRIFT_REASON}]

        _op.archive_and_commit = _refusing_move
        result = self._run_act()

        self.assertIs(result.get("archived"), False)
        self.assertIs(result.get("already_archived"), False)
        self.assertEqual(
            [item.get("reason") for item in result.get("failed") or []],
            [_DRIFT_REASON],
            f"the mover's own reason must be on the wire; got {result!r}",
        )

    def test_no_accumulator_stays_vacuous_and_carries_no_failed_key(self):
        result = self._run_act()

        self.assertIs(result.get("archived"), False)
        self.assertIs(result.get("already_archived"), True)
        self.assertNotIn("failed", result)

    def test_setup_error_is_distinguishable_from_a_refusal(self):
        result = asyncio.run(
            _op._handler({"tag": "", "dry_run": False}, self.worktree / ".git")
        )

        self.assertIs(result.get("archived"), False)
        self.assertIs(result.get("already_archived"), False)
        self.assertNotIn(
            "failed", result, "a setup error is not a refused move"
        )

    def test_successful_move_carries_no_failed_key(self):
        self._seed_accumulator()

        async def _accepting_move(worktree, moves, subject):
            return [{"id": moves[0].candidate_id, "archived": True}], []

        _op.archive_and_commit = _accepting_move
        result = self._run_act()

        self.assertIs(result.get("archived"), True)
        self.assertNotIn("failed", result)


if __name__ == "__main__":
    unittest.main()
