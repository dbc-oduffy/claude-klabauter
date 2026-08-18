"""Tests for coordinator_core.ops.backfill_reference_edges —
"fleet.backfill_reference_edges", the one-shot corpus backfill from chunk C7
(docs/plans/2026-08-18-a-session-always-has-a-baton.md § D-D).

Coverage:
  - dry_run reports would_stamp without writing anything to disk;
  - a real run stamps `references:` onto the referrer, resolving the
    referenced target's own `plan_id`/`handoff_id`;
  - idempotent: running twice does not duplicate an edge, and the second
    real run's `stamped` list is empty (nothing new to add);
  - a referrer whose body cites no other artifact by filename is skipped,
    not touched;
  - a filename-only citation with no resolvable id on the target is never
    fabricated into an edge;
  - reachability: "fleet.backfill_reference_edges" resolves in the live
    op registry.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import yaml

from coordinator_core.ops import backfill_reference_edges as _op


def _write(path: Path, frontmatter: dict, body: str = "body\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_text = yaml.safe_dump(frontmatter, default_flow_style=False, sort_keys=False)
    path.write_text(f"---\n{fm_text}---\n{body}", encoding="utf-8")


class BackfillReferenceEdgesTest(unittest.TestCase):
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

    def test_dry_run_reports_without_writing(self):
        target = self.plans_dir / "2026-01-01-target-plan.md"
        _write(target, {"status": "implemented", "plan_id": "pln-target-abc123"})

        referrer = self.plans_dir / "2026-01-02-referrer-plan.md"
        _write(
            referrer,
            {"status": "executing"},
            body="See docs/plans/2026-01-01-target-plan.md for background.\n",
        )

        result = _op._run_backfill(self.worktree, dry_run=True)

        self.assertEqual(result["stamped"], [])
        self.assertEqual(len(result["would_stamp"]), 1)
        self.assertEqual(result["would_stamp"][0]["id"], referrer.name)
        self.assertEqual(result["would_stamp"][0]["references_added"], ["pln-target-abc123"])

        # Nothing written to disk.
        raw = referrer.read_text(encoding="utf-8")
        self.assertNotIn("references:", raw)

    def test_real_run_stamps_the_resolved_id(self):
        target = self.plans_dir / "2026-01-01-target-plan.md"
        _write(target, {"status": "implemented", "plan_id": "pln-target-abc123"})

        referrer = self.plans_dir / "2026-01-02-referrer-plan.md"
        _write(
            referrer,
            {"status": "executing"},
            body="See docs/plans/2026-01-01-target-plan.md for background.\n",
        )

        result = _op._run_backfill(self.worktree, dry_run=False)

        self.assertEqual(len(result["stamped"]), 1)
        self.assertEqual(result["stamped"][0]["id"], referrer.name)

        raw = referrer.read_text(encoding="utf-8")
        split = yaml.safe_load(raw.split("---", 2)[1])
        self.assertEqual(split.get("references"), ["pln-target-abc123"])

    def test_idempotent_second_run_adds_nothing(self):
        target = self.plans_dir / "2026-01-01-target-plan.md"
        _write(target, {"status": "implemented", "plan_id": "pln-target-abc123"})

        referrer = self.plans_dir / "2026-01-02-referrer-plan.md"
        _write(
            referrer,
            {"status": "executing"},
            body="See docs/plans/2026-01-01-target-plan.md for background.\n",
        )

        first = _op._run_backfill(self.worktree, dry_run=False)
        self.assertEqual(len(first["stamped"]), 1)

        second = _op._run_backfill(self.worktree, dry_run=False)
        self.assertEqual(second["stamped"], [])
        self.assertEqual(
            [s["id"] for s in second["skipped"]], [referrer.name]
        )

        raw = referrer.read_text(encoding="utf-8")
        split = yaml.safe_load(raw.split("---", 2)[1])
        # Still exactly one entry — not duplicated.
        self.assertEqual(split.get("references"), ["pln-target-abc123"])

    def test_referrer_with_no_citation_is_skipped(self):
        target = self.plans_dir / "2026-01-01-target-plan.md"
        _write(target, {"status": "implemented", "plan_id": "pln-target-abc123"})

        referrer = self.plans_dir / "2026-01-02-referrer-plan.md"
        _write(referrer, {"status": "executing"}, body="unrelated body text\n")

        result = _op._run_backfill(self.worktree, dry_run=False)
        self.assertEqual(result["stamped"], [])
        self.assertEqual(
            [s["id"] for s in result["skipped"]], [referrer.name]
        )

    def test_filename_citation_with_no_target_id_is_never_fabricated(self):
        target = self.plans_dir / "2026-01-01-target-plan.md"
        _write(target, {"status": "implemented"})  # no plan_id minted

        referrer = self.plans_dir / "2026-01-02-referrer-plan.md"
        _write(
            referrer,
            {"status": "executing"},
            body="See docs/plans/2026-01-01-target-plan.md for background.\n",
        )

        result = _op._run_backfill(self.worktree, dry_run=False)
        self.assertEqual(result["stamped"], [])
        raw = referrer.read_text(encoding="utf-8")
        self.assertNotIn("references:", raw)

    def test_handoff_referrer_resolves_plan_target(self):
        target = self.plans_dir / "2026-01-01-target-plan.md"
        _write(target, {"status": "implemented", "plan_id": "pln-target-abc123"})

        referrer = self.handoffs_dir / "2026-01-02-referrer-handoff.md"
        _write(
            referrer,
            {"status": "open"},
            body="Carried forward from docs/plans/2026-01-01-target-plan.md.\n",
        )

        result = _op._run_backfill(self.worktree, dry_run=False)
        self.assertEqual(len(result["stamped"]), 1)
        self.assertEqual(result["stamped"][0]["id"], referrer.name)

    def test_handler_reachable_in_op_registry(self):
        from coordinator_core import ipc
        import coordinator_core.ops  # noqa: F401 — trigger eager registration

        self.assertIn("fleet.backfill_reference_edges", ipc._REGISTRY)

    def test_handler_dry_run_default_true(self):
        target = self.plans_dir / "2026-01-01-target-plan.md"
        _write(target, {"status": "implemented", "plan_id": "pln-target-abc123"})
        referrer = self.plans_dir / "2026-01-02-referrer-plan.md"
        _write(
            referrer,
            {"status": "executing"},
            body="See docs/plans/2026-01-01-target-plan.md for background.\n",
        )

        result = asyncio.run(_op._handler({}, repo_root=self.common_dir))
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["stamped"], [])
        self.assertEqual(len(result["would_stamp"]), 1)
        raw = referrer.read_text(encoding="utf-8")
        self.assertNotIn("references:", raw)

    def test_handler_missing_repo_root_refuses(self):
        result = asyncio.run(_op._handler({}, repo_root=None))
        self.assertEqual(result["exit_code"], 1)


if __name__ == "__main__":
    unittest.main()
