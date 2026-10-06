"""supersede over an on-disk deployment_state:shipped baton is refused.

The refusal sits at the handler's supersede choke point, before any stamp, so
the file stays byte-identical. A baton merely claimed (no shipped
deployment_state) still passes this gate.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from coordinator_core.ops import handoff_archive_transition as _op


class SupersedeShippedRefusedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.worktree = Path(self._tmp.name).resolve()
        (self.worktree / ".git").mkdir()
        (self.worktree / "state" / "handoffs").mkdir(parents=True)
        self.path = self.worktree / "state" / "handoffs" / "candidate.md"

    def _seed(self, deployment_state: str) -> bytes:
        self.path.write_text(
            "---\n"
            "status: claimed\n"
            f"deployment_state: {deployment_state}\n"
            "claimed_by: sess-1\n"
            "---\nbody\n",
            encoding="utf-8",
        )
        return self.path.read_bytes()

    def _supersede(self) -> dict:
        return asyncio.run(
            _op._handler(
                {
                    "handoff_path": str(self.path),
                    "mode": "supersede",
                    "continued_into": "state/handoffs/successor.md",
                },
                self.worktree / ".git",
            )
        )

    def test_shipped_baton_is_refused_and_file_is_byte_identical(self):
        before = self._seed("shipped")
        result = self._supersede()

        self.assertEqual(result.get("exit_code"), 1)
        self.assertIs(result.get("superseded"), False)
        self.assertIs(result.get("stamped"), False)
        self.assertIs(result.get("choke_point_refusal"), True)
        self.assertIn("shipped", result.get("error", ""))
        self.assertEqual(self.path.read_bytes(), before)

    def test_closed_baton_is_still_refused_by_its_own_gate(self):
        before = self._seed("closed")
        result = self._supersede()

        self.assertIs(result.get("choke_point_refusal"), True)
        self.assertIn("closed", result.get("error", ""))
        self.assertEqual(self.path.read_bytes(), before)

    def test_claimed_unshipped_baton_is_not_refused_by_the_shipped_gate(self):
        self._seed("in_flight")
        result = self._supersede()

        self.assertNotIn("is deployment_state: shipped", result.get("error") or "")


if __name__ == "__main__":
    unittest.main()
