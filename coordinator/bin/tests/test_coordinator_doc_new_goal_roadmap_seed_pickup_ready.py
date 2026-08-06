"""test_coordinator_doc_new_goal_roadmap_seed_pickup_ready.py -- coverage for
the break-class defect reported by cross-repo memo
2026-08-06-example-market-data-repo-em-pickup-ready-true-under-unmet-gate.md:
the `goal-seed` and `roadmap-seed` scaffold arms used to write both
`deployment_state: awaiting_gate` and `pickup_ready: true` as adjacent,
un-branched literals -- a baton advertising itself fire-ready while its own
gate is unmet. `pickup_ready` is now OMITTED entirely from both arms, per the
established OMITTED-pickup_ready idiom already documented on
`_scaffold_roadmap_baton` ("absence triggers a non-blocking /pickup warn").

The other three arms (session-handoff, recovery, spinoff) pair
`pickup_ready: true` with `ready_to_fire`, which is coherent and unchanged.

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as test_coordinator_doc_new_predecessor.py.

Spec backlink: cross-repo/inbox/2026-08-06-example-market-data-repo-em-pickup-ready-true-under-unmet-gate.md
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent
_CLI_PATH = _BIN_DIR / "coordinator-doc-new"


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_goal_roadmap_seed_pickup_ready_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_goal_roadmap_seed_pickup_ready_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class GoalSeedRoadmapSeedOmitPickupReadyTest(unittest.TestCase):
    def test_goal_seed_omits_pickup_ready(self):
        content = _cli._scaffold_goal_seed(title="t", branch="b")
        self.assertNotIn("pickup_ready", content)

    def test_roadmap_seed_omits_pickup_ready(self):
        content = _cli._scaffold_roadmap_seed(title="t", branch="b")
        self.assertNotIn("pickup_ready", content)

    def test_goal_seed_still_awaiting_gate(self):
        """Non-regression: only pickup_ready was removed, not the gate axis."""
        content = _cli._scaffold_goal_seed(title="t", branch="b")
        self.assertIn("deployment_state: awaiting_gate", content)

    def test_roadmap_seed_still_awaiting_gate(self):
        content = _cli._scaffold_roadmap_seed(title="t", branch="b")
        self.assertIn("deployment_state: awaiting_gate", content)


class CoherentArmsStillEmitPickupReadyTrueTest(unittest.TestCase):
    """The other three arms pair pickup_ready: true with ready_to_fire --
    this fix must not touch them."""

    def test_handoff_still_emits_pickup_ready_true(self):
        content = _cli._scaffold_handoff(title="t", branch="b")
        self.assertIn("pickup_ready: true", content)

    def test_recovery_still_emits_pickup_ready_true(self):
        content = _cli._scaffold_recovery(title="t", branch="b", recovers_session="sid-example")
        self.assertIn("pickup_ready: true", content)

    def test_spinoff_still_emits_pickup_ready_true(self):
        content = _cli._scaffold_spinoff(title="t", branch="b")
        self.assertIn("pickup_ready: true", content)


if __name__ == "__main__":
    unittest.main()
