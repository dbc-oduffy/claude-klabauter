"""test_archive_stamp_cli_stamp_plan_superseded.py — argv-parsing +
engine-behavior test for `archive-stamp-cli stamp-plan-superseded` (C9,
docs/plans/2026-09-11-vendored-schemas-and-the-work-state-contract.md).

Defect this closes: `_stamp_superseded` (`coordinator_core/ops/
plan_status_transition.py :: main`, verb `stamp-superseded`, `--by` required)
existed but was reachable only as
`python -m coordinator_core.ops.plan_status_transition` — the cockpit memo's
"no superseded exit" gap. `cs_stamp_plan_superseded` (archive_stamp.py) and
this CLI's `stamp-plan-superseded` subcommand are the missing veneer, added
via the exact in-process route `cs_stamp_plan_implemented` already takes.

Two halves, deliberately not one:
  - `StampPlanSupersededArgvParsingTest` — the `_import_module()` seam is
    monkeypatched (same idiom as test_archive_stamp_cli_close_handoff.py) so
    argv -> `cs_stamp_plan_superseded(...)` call-shape translation is asserted
    without a real claude-klabauter checkout / resolvable engine root.
  - `StampPlanSupersededEngineBehaviorTest` — calls
    `coordinator_core.archive_stamp.cs_stamp_plan_superseded` directly
    (imported straight off this checkout, bypassing the CLI's
    `require_dispatch_engine_on_path` bootstrap entirely — same reason
    `coordinator_core/test_archive_stamp.py`'s own
    `TestStampPlanImplemented` needs no bootstrap) to prove the real
    frontmatter-write contract: draft -> superseded + superseded_by; an
    already-superseded plan no-ops at rc 0; an already-implemented plan is
    REFUSED at rc 1 with the file byte-unchanged (`_stamp_superseded`'s
    `_FROZEN_STATUSES` branch only no-ops on "already superseded" — any
    OTHER frozen status, "implemented" included, raises `MutateAbort`, rc 1
    — see `coordinator_core/ops/plan_status_transition.py ::
    _stamp_superseded`'s own docstring/body; the original C9 spec wording
    asserted a no-op for "already-implemented", which this op refuses by
    design, per Review: coordinator:eng-director / coordinator:staff-eng
    on the plan-spine row).

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`archive-stamp-cli` is an extensionless polyglot entrypoint, not a `.py`
module — same load idiom as test_archive_stamp_cli_close_handoff.py.

Run:
    pytest coordinator/bin/tests/test_archive_stamp_cli_stamp_plan_superseded.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BIN_DIR.parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "archive_stamp_cli_stamp_plan_superseded_test", str(_BIN_DIR / "archive-stamp-cli.py")
    )
    spec = importlib.util.spec_from_loader(
        "archive_stamp_cli_stamp_plan_superseded_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _RecordingStampPlanSupersededMod:
    """Stand-in for coordinator_core.archive_stamp — records the exact
    args cs_stamp_plan_superseded was called with, so each test can assert
    the argv -> call-shape translation without a real claude-klabauter checkout."""

    def __init__(self, rc: int = 0):
        self.calls: list[dict] = []
        self._rc = rc

    def cs_stamp_plan_superseded(self, plan_path, by):
        self.calls.append({"plan_path": plan_path, "by": by})
        return self._rc


class StampPlanSupersededArgvParsingTest(unittest.TestCase):
    def setUp(self):
        self._orig_import_module = _cli._import_module
        self.addCleanup(self._restore)
        self.stub = _RecordingStampPlanSupersededMod()
        _cli._import_module = lambda: self.stub

    def _restore(self):
        _cli._import_module = self._orig_import_module

    def test_by_flag_is_forwarded(self):
        rc = _cli.main(
            ["stamp-plan-superseded", "docs/plans/x.md", "--by", "docs/plans/y.md"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stub.calls[-1],
            {"plan_path": "docs/plans/x.md", "by": "docs/plans/y.md"},
        )

    def test_missing_by_flag_is_usage_error_no_engine_call(self):
        rc = _cli.main(["stamp-plan-superseded", "docs/plans/x.md"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

    def test_by_flag_missing_value_is_usage_error_no_engine_call(self):
        rc = _cli.main(["stamp-plan-superseded", "docs/plans/x.md", "--by"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

    def test_missing_plan_path_is_usage_error_no_engine_call(self):
        rc = _cli.main(["stamp-plan-superseded"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.calls, [])

    def test_engine_refusal_propagates_verbatim(self):
        """A CLI-layer PASS (--by supplied) must still propagate an
        engine-layer refusal (e.g. an already-terminal-at-a-different-status
        plan) rather than masking it — the CLI does NOT re-validate the
        op's own gate; cs_stamp_plan_superseded / _stamp_superseded is the
        single authoritative refusal."""
        _cli._import_module = lambda: _RecordingStampPlanSupersededMod(rc=1)
        rc = _cli.main(
            ["stamp-plan-superseded", "docs/plans/x.md", "--by", "docs/plans/y.md"]
        )
        self.assertEqual(rc, 1)

    def test_stamp_plan_superseded_has_a_usage_entry(self):
        self.assertIn("stamp-plan-superseded", _cli._SUBCOMMAND_USAGE)
        self.assertIn("--by", _cli._SUBCOMMAND_USAGE["stamp-plan-superseded"])


class StampPlanSupersededEngineBehaviorTest(unittest.TestCase):
    """Exercises the real coordinator_core.archive_stamp.cs_stamp_plan_superseded
    -> plan_status_transition.main native in-process route directly — no CLI
    layer, no mocked engine."""

    def setUp(self):
        import sys

        if str(_REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(_REPO_ROOT))
        import coordinator_core.archive_stamp as arstamp

        self.arstamp = arstamp

    def _seed_plan(self, tmp_path: Path, status: str) -> Path:
        plan = tmp_path / "plan.md"
        plan.write_text(f"---\nstatus: {status}\n---\n\nBody.\n", encoding="utf-8")
        successor = tmp_path / "successor.md"
        successor.write_text("---\nstatus: draft\n---\n\nSuccessor.\n", encoding="utf-8")
        return plan

    def test_draft_plan_gains_superseded_and_superseded_by(self, tmp_path=None):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            plan = self._seed_plan(tmp_path, "draft")
            successor = tmp_path / "successor.md"
            rc = self.arstamp.cs_stamp_plan_superseded(str(plan), str(successor))
            self.assertEqual(rc, 0)
            text = plan.read_text(encoding="utf-8")
            self.assertIn("status: superseded", text)
            self.assertIn(f"superseded_by: {successor}", text)

    def test_missing_by_is_usage_error_no_write(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            plan = self._seed_plan(tmp_path, "draft")
            before = plan.read_text(encoding="utf-8")
            rc = self.arstamp.cs_stamp_plan_superseded(str(plan), "")
            self.assertEqual(rc, 1)
            self.assertEqual(plan.read_text(encoding="utf-8"), before)

    def test_already_superseded_plan_is_a_noop(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            plan = self._seed_plan(tmp_path, "superseded")
            successor = tmp_path / "successor.md"
            before = plan.read_text(encoding="utf-8")
            rc = self.arstamp.cs_stamp_plan_superseded(str(plan), str(successor))
            self.assertEqual(rc, 0)
            self.assertEqual(plan.read_text(encoding="utf-8"), before)

    def test_already_implemented_plan_is_refused_file_unchanged(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            plan = self._seed_plan(tmp_path, "implemented")
            successor = tmp_path / "successor.md"
            before = plan.read_text(encoding="utf-8")
            rc = self.arstamp.cs_stamp_plan_superseded(str(plan), str(successor))
            self.assertEqual(rc, 1)
            self.assertEqual(plan.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
