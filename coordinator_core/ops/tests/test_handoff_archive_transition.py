"""
coordinator_core.ops.tests.test_handoff_archive_transition

C4 of docs/plans/2026-08-13-archive-family-coverage-restoration.md.

DISPOSITION, verified against HEAD 2026-09-19 (AC7b): the culled file's 53
`def test_` functions are NOT ported wholesale — reading and re-deriving
each against the current 4-mode `_handler` (chain / stamp_shipped /
stamp_only / supersede; the "faithful native-Python port of DoE's
coordinator-handoff-archive.sh" this op's own docstring names) is a job this
row's remaining budget does not cover, and Anti-scope forbids porting an
assertion unread. What is ported here is a new, deliberately-scoped slice:
the mode-dispatch/usage-error contract every one of the 53 culled tests sat
downstream of, plus one mocked-mover happy path proving the dispatch
actually reaches `archive_and_commit` for the terminal case. This is git-
free (0 real-git test functions, AC5-neutral) by construction — every test
below monkeypatches `archive_and_commit` in this op's own namespace, mirrors
`test_archive_transition_refusal_reason.py`'s existing style (same package,
same seam), and never spawns git.

**Existing coverage this module does NOT re-derive:** `test_archive_
transition_refusal_reason.py` (a refused move's own reason reaching the
wire), `test_archive_transition_attested_succession.py` (mode="supersede"'s
DR-242 attested-succession clauses), `test_handoff_archive_transition_
holder_live.py` (mode-scoped live-holder retention). Those three already
narrowly cover real slices of the 53-test population; this module covers
the mode-dispatch entry surface none of them target.

**Remainder undispositioned, named for C5(a)'s roster rather than silently
absorbed:** the bulk of the 53 — mode="stamp_shipped"/"supersede" full
stamp-then-archive flows, the Position-A no-branch-tip-fallback rule, the
`successor_path` resolution path, the archived-predecessor stamp-in-place
branch (mode="supersede" against an already-archived source), and the
`_commit_retained_supersede_flip` real-git ls-tree check — remain unread
against current source and are NOT claimed as covered by this row. They are
a genuine shortfall against the culled count, to be dispositioned (port,
rewrite, or drop) by a future chunk, not this one.

Spec backlinks:
  - Op under test: coordinator_core/ops/handoff_archive_transition.py
  - Seam/style precedent: coordinator_core/ops/tests/
    test_archive_transition_refusal_reason.py
  - This chunk: docs/plans/2026-08-13-archive-family-coverage-restoration.md, C4

AC5b — not applicable (0 real-git test functions in this module).
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from coordinator_core.ops import handoff_archive_transition as _op


class _BaseCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.worktree = Path(self._tmp.name).resolve()
        (self.worktree / ".git").mkdir()
        (self.worktree / "state" / "handoffs").mkdir(parents=True)
        self.handoff_path = self.worktree / "state" / "handoffs" / "candidate.md"

    def _seed(self, deployment_state: str) -> None:
        self.handoff_path.write_text(
            "---\n"
            "status: claimed\n"
            f"deployment_state: {deployment_state}\n"
            "---\nbody\n",
            encoding="utf-8",
        )

    def _call(self, params: dict) -> dict:
        return asyncio.run(_op._handler(params, self.worktree / ".git"))


class ModeDispatchUsageErrorsTest(_BaseCase):
    """The five usage-error branches that gate `_handler` before any real
    move is attempted — validated ahead of `handoff_path`/`repo_root`
    resolution, so no seeded file or worktree state is needed for most of
    them."""

    def test_unknown_mode_is_a_usage_error(self):
        result = self._call({"handoff_path": "x", "mode": "bogus"})
        self.assertEqual(result.get("exit_code"), 2)
        self.assertIn("unknown mode", result.get("error", ""))

    def test_supersede_without_continued_into_is_a_usage_error(self):
        result = self._call({"handoff_path": "x", "mode": "supersede"})
        self.assertEqual(result.get("exit_code"), 2)
        self.assertIn("continued_into", result.get("error", ""))

    def test_force_without_sha_is_a_usage_error(self):
        result = self._call({"handoff_path": "x", "mode": "chain", "force": True})
        self.assertEqual(result.get("exit_code"), 2)
        self.assertIn("'force' requires 'sha'", result.get("error", ""))

    def test_kind_without_sha_is_a_usage_error(self):
        result = self._call({"handoff_path": "x", "mode": "chain", "kind": "successor"})
        self.assertEqual(result.get("exit_code"), 2)
        self.assertIn("'kind' requires 'sha'", result.get("error", ""))

    def test_successor_path_mutually_exclusive_with_sha(self):
        result = self._call({
            "handoff_path": "x", "mode": "chain",
            "successor_path": "state/handoffs/other.md", "sha": "abc1234",
        })
        self.assertEqual(result.get("exit_code"), 2)
        self.assertIn("mutually exclusive", result.get("error", ""))

    def test_mode_omitted_defaults_to_chain(self):
        # handoff_path also missing -> the early _err short-circuit still
        # tags the resolved default mode onto the envelope, cheaply proving
        # the "chain" default without needing a seeded repo.
        result = self._call({})
        self.assertEqual(result.get("mode"), "chain")


class ChainModeMovedDispatchTest(_BaseCase):
    """mode="chain" against a terminal (shipped) candidate reaches
    `archive_and_commit` and reports the mover's own outcome verbatim —
    proving the dispatch path itself (containment, terminal-state gate,
    Move construction) without asserting anything about the mover's real
    git mechanics (that is `coordinator_core/ops/fleet/tests/test_archive_
    and_commit_disk_head_drift.py` and this file's sibling `test_archive_
    and_commit_call_site_coverage.py`'s remit, not this op's)."""

    def setUp(self):
        super().setUp()
        self._seed("shipped")
        self._orig_move = _op.archive_and_commit
        self.addCleanup(self._restore)

    def _restore(self):
        _op.archive_and_commit = self._orig_move

    def test_terminal_candidate_reaches_the_mover_and_reports_moved(self):
        async def _accepting_move(worktree, moves, subject):
            self.assertEqual(len(moves), 1)
            self.assertEqual(moves[0].candidate_id, "state/handoffs/candidate.md")
            return [{"id": moves[0].candidate_id, "archived": True}], []

        _op.archive_and_commit = _accepting_move
        result = self._call({
            "handoff_path": str(self.handoff_path), "mode": "chain",
        })

        self.assertIs(result.get("moved"), True)
        self.assertEqual(result.get("mode"), "chain")

    def test_non_terminal_candidate_is_refused_before_reaching_the_mover(self):
        self._seed("in_flight")

        async def _unreached_move(worktree, moves, subject):
            raise AssertionError("archive_and_commit must not be called for a non-terminal candidate")

        _op.archive_and_commit = _unreached_move
        result = self._call({
            "handoff_path": str(self.handoff_path), "mode": "chain",
        })

        self.assertEqual(result.get("exit_code"), 1)
        self.assertIn("not terminal", result.get("error", ""))


if __name__ == "__main__":
    unittest.main()
