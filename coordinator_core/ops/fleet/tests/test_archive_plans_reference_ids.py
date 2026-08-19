"""Tests for the ID-resolved live-reference sibling added in chunk C7
(docs/plans/2026-08-18-a-session-always-has-a-baton.md § D-D).

`_collect_live_reference_ids` / `_plan_is_id_referenced` are the new
frontmatter-only resolution path, consulted ALONGSIDE
`_collect_live_reference_text` — never instead of it. This file pins:

  - the ID-resolved path retains a plan whose `plan_id` is named in a live
    referrer's `references:` list, even when no filename substring match
    exists (the case the substring scan alone cannot catch);
  - the substring scan stays independently authoritative — a plan retained
    only by filename substring (no `references:` anywhere) is untouched;
  - `references:` absence is the common case and reads as "not referenced by
    this mechanism", not an error;
  - `scan_incomplete` propagates from the id-scan the same way it does from
    the text-scan (fail-closed union).

NOTE (deviation from the chunk's named test surface): the chunk names
`coordinator_core/ops/fleet/tests/test_archive_plans.py` as the test surface.
That file was deleted by a prior "test cull: delete the spawn-heavy
Windows-poison test set from orbit" commit (1d4e686a9) and does not exist in
this tree. This file is added alongside the two other post-cull
archive_plans test files (`test_archive_plans_backlink_gate_wiring.py`,
`test_archive_plans_sidecar_refusal.py`) rather than resurrecting the culled
one, matching their pattern: helper functions and monkeypatched seams,
no git spawn, no corpus scan via subprocess.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from coordinator_core.ops.fleet import archive_plans as _op


class CollectLiveReferenceIdsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.worktree = Path(self._tmp.name).resolve()
        self.plans_dir = self.worktree / "docs" / "plans"
        self.plans_dir.mkdir(parents=True)
        self.handoffs_dir = self.worktree / "state" / "handoffs"
        self.handoffs_dir.mkdir(parents=True)

    def test_id_referenced_plan_is_retained_with_no_filename_substring(self):
        """A plan named ONLY via a stamped `references:` edge (no filename
        substring anywhere in the live corpus) is retained by the ID path."""
        terminal = self.plans_dir / "2026-01-01-terminal-plan.md"
        terminal.write_text(
            "---\nstatus: implemented\nplan_id: pln-terminal-abc123\n---\nbody\n",
            encoding="utf-8",
        )
        live = self.plans_dir / "2026-01-02-live-plan.md"
        live.write_text(
            "---\nstatus: executing\nreferences:\n  - pln-terminal-abc123\n---\n"
            "body mentions nothing by filename\n",
            encoding="utf-8",
        )

        live_ref_ids, scan_incomplete = _op._collect_live_reference_ids(self.worktree)
        self.assertFalse(scan_incomplete)
        self.assertIn("pln-terminal-abc123", live_ref_ids)
        self.assertTrue(_op._plan_is_id_referenced(terminal, live_ref_ids))

        # Substring scan independently finds nothing (filename never cited).
        live_ref_text, _ = _op._collect_live_reference_text(self.worktree)
        self.assertNotIn(terminal.name, live_ref_text)

    def test_no_references_field_is_the_common_case_not_an_error(self):
        terminal = self.plans_dir / "2026-01-01-terminal-plan.md"
        terminal.write_text(
            "---\nstatus: implemented\nplan_id: pln-terminal-abc123\n---\nbody\n",
            encoding="utf-8",
        )
        live = self.plans_dir / "2026-01-02-live-plan.md"
        live.write_text("---\nstatus: executing\n---\nno references field at all\n", encoding="utf-8")

        live_ref_ids, scan_incomplete = _op._collect_live_reference_ids(self.worktree)
        self.assertFalse(scan_incomplete)
        self.assertEqual(live_ref_ids, set())
        self.assertFalse(_op._plan_is_id_referenced(terminal, live_ref_ids))

    def test_terminal_plan_excluded_as_a_referrer_source(self):
        """A TERMINAL plan's own `references:` list is not a live edge —
        only live plans/handoffs contribute ids, mirroring the text scan's
        live-only referrer set."""
        terminal_target = self.plans_dir / "2026-01-01-target.md"
        terminal_target.write_text(
            "---\nstatus: implemented\nplan_id: pln-target-abc123\n---\nbody\n",
            encoding="utf-8",
        )
        terminal_referrer = self.plans_dir / "2026-01-02-terminal-referrer.md"
        terminal_referrer.write_text(
            "---\nstatus: implemented\nreferences:\n  - pln-target-abc123\n---\nbody\n",
            encoding="utf-8",
        )

        live_ref_ids, _ = _op._collect_live_reference_ids(self.worktree)
        self.assertEqual(live_ref_ids, set())

    def test_retired_handoff_excluded_as_a_referrer_source(self):
        terminal_target = self.plans_dir / "2026-01-01-target.md"
        terminal_target.write_text(
            "---\nstatus: implemented\nplan_id: pln-target-abc123\n---\nbody\n",
            encoding="utf-8",
        )
        claimed_handoff = self.handoffs_dir / "2026-01-02-claimed.md"
        claimed_handoff.write_text(
            "---\nstatus: claimed\nreferences:\n  - pln-target-abc123\n---\nbody\n",
            encoding="utf-8",
        )

        live_ref_ids, _ = _op._collect_live_reference_ids(self.worktree)
        self.assertEqual(live_ref_ids, set())

    def test_live_handoff_reference_is_collected(self):
        terminal_target = self.plans_dir / "2026-01-01-target.md"
        terminal_target.write_text(
            "---\nstatus: implemented\nplan_id: pln-target-abc123\n---\nbody\n",
            encoding="utf-8",
        )
        open_handoff = self.handoffs_dir / "2026-01-02-open.md"
        open_handoff.write_text(
            "---\nstatus: open\nreferences:\n  - pln-target-abc123\n---\nbody\n",
            encoding="utf-8",
        )

        live_ref_ids, _ = _op._collect_live_reference_ids(self.worktree)
        self.assertIn("pln-target-abc123", live_ref_ids)
        self.assertTrue(_op._plan_is_id_referenced(terminal_target, live_ref_ids))

    def test_plan_with_no_plan_id_never_matches_id_path(self):
        """A plan minted without a plan_id (pre-C2 record) cannot be
        ID-referenced — it stays covered by the substring scan alone."""
        terminal = self.plans_dir / "2026-01-01-no-id.md"
        terminal.write_text("---\nstatus: implemented\n---\nbody\n", encoding="utf-8")
        live = self.plans_dir / "2026-01-02-live.md"
        live.write_text(
            "---\nstatus: executing\nreferences:\n  - pln-some-other-id\n---\nbody\n",
            encoding="utf-8",
        )

        live_ref_ids, _ = _op._collect_live_reference_ids(self.worktree)
        self.assertFalse(_op._plan_is_id_referenced(terminal, live_ref_ids))


class PreviewAndActUseIdPathAlongsideTextScanTest(unittest.TestCase):
    """End-to-end (still no subprocess/git spawn — guards monkeypatched)
    check that _handle_preview/_handle_act retain a plan that ONLY the
    ID-resolved edge covers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.worktree = Path(self._tmp.name).resolve()
        self.common_dir = self.worktree / ".git"
        self.common_dir.mkdir()
        self.plans_dir = self.worktree / "docs" / "plans"
        self.plans_dir.mkdir(parents=True)
        self.handoffs_dir = self.worktree / "state" / "handoffs"
        self.handoffs_dir.mkdir(parents=True)

        self._orig = {
            "dirty": _op._plan_worktree_dirty,
            "dirty_batch": _op._plan_worktree_dirty_batch,
            "claim": _op._plan_claim_live,
        }
        self.addCleanup(self._restore)

        async def _not_dirty(worktree_root, rel_path):
            return False

        async def _not_dirty_batch(worktree_root, rel_paths):
            return set()

        async def _not_claimed(common_dir, plan_path):
            return False

        _op._plan_worktree_dirty = _not_dirty
        _op._plan_worktree_dirty_batch = _not_dirty_batch
        _op._plan_claim_live = _not_claimed

    def _restore(self):
        _op._plan_worktree_dirty = self._orig["dirty"]
        _op._plan_worktree_dirty_batch = self._orig["dirty_batch"]
        _op._plan_claim_live = self._orig["claim"]

    def test_preview_skips_a_plan_referenced_only_by_stamped_id(self):
        terminal = self.plans_dir / "2026-01-01-terminal-plan.md"
        terminal.write_text(
            "---\nstatus: implemented\nplan_id: pln-terminal-abc123\n---\nbody\n",
            encoding="utf-8",
        )
        live = self.plans_dir / "2026-01-02-live-plan.md"
        live.write_text(
            "---\nstatus: executing\nreferences:\n  - pln-terminal-abc123\n---\n"
            "body mentions nothing by filename\n",
            encoding="utf-8",
        )

        result = asyncio.run(
            _op._handle_preview("archive", self.worktree, self.plans_dir, self.common_dir)
        )
        candidate_ids = {c["id"] for c in result["candidates"]}
        self.assertNotIn("docs/plans/2026-01-01-terminal-plan.md", candidate_ids)

    def test_act_skips_a_plan_referenced_only_by_stamped_id(self):
        terminal = self.plans_dir / "2026-01-01-terminal-plan.md"
        terminal.write_text(
            "---\nstatus: implemented\nplan_id: pln-terminal-abc123\n---\nbody\n",
            encoding="utf-8",
        )
        live = self.plans_dir / "2026-01-02-live-plan.md"
        live.write_text(
            "---\nstatus: executing\nreferences:\n  - pln-terminal-abc123\n---\n"
            "body mentions nothing by filename\n",
            encoding="utf-8",
        )

        result = asyncio.run(
            _op._handle_act(
                "archive",
                self.worktree,
                self.plans_dir,
                ["docs/plans/2026-01-01-terminal-plan.md"],
                self.common_dir,
            )
        )
        skipped_reasons = {s["id"]: s["reason"] for s in result["skipped"]}
        self.assertEqual(
            skipped_reasons.get("docs/plans/2026-01-01-terminal-plan.md"), "live-reference"
        )
        self.assertEqual(result["acted"], [])


if __name__ == "__main__":
    unittest.main()
