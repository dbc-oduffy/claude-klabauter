"""A baton minted from a sizing omits unfilled sections and a stale next step; a plain spinoff is unchanged."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import unittest
from pathlib import Path
from unittest import mock

_CLI_PATH = Path(__file__).resolve().parents[1] / "coordinator-doc-new.py"


def _load_cli():
    name = "coordinator_doc_new_minted_baton_omits_unfilled_test"
    loader = importlib.machinery.SourceFileLoader(name, str(_CLI_PATH))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


_cli = _load_cli()
_ENV = {"COORDINATOR_SESSION_ID": "11111111-2222-3333-4444-555555555555"}


def _spinoff(**kw) -> str:
    with mock.patch.dict(os.environ, _ENV):
        return _cli._scaffold_spinoff(title="t", branch="b", kind="session-handoff", **kw)


class OmitUnfilledTest(unittest.TestCase):
    def test_empty_reference_and_anti_scope_omitted(self):
        body = _spinoff(omit_unfilled=True)
        self.assertNotIn("## Reference materials", body)
        self.assertNotIn("## Anti-scope", body)

    def test_filled_reference_materials_kept(self):
        body = _spinoff(omit_unfilled=True, reference_materials=["- a.md -- why"])
        self.assertIn("## Reference materials", body)
        self.assertIn("- a.md -- why", body)

    def test_ledger_block_omitted_entirely(self):
        body = _spinoff(omit_unfilled=True)
        self.assertNotIn("## Session Ledger", body)
        self.assertNotIn("Phase 2 LoE accumulator", body)

    def test_plain_spinoff_unchanged(self):
        body = _spinoff()
        self.assertIn("## Reference materials (read first)", body)
        self.assertIn("## Anti-scope", body)
        self.assertIn("<!-- Phase 2 LoE accumulator", body)


class NextStepTest(unittest.TestCase):
    def test_empty_plan_edge_step_is_true_either_way(self):
        _, steps = _cli._sizing_remit_and_steps({"route": "plan"}, "intent", "state/sizings/x.yaml", None)
        self.assertNotIn("Author the plan for the intent above", steps[0])
        self.assertIn("`plan:` edge", steps[0])

    def test_named_plan_is_read(self):
        _, steps = _cli._sizing_remit_and_steps(
            {"route": "plan"}, "intent", "state/sizings/x.yaml", "docs/plans/p.md"
        )
        self.assertEqual(steps[0], "1. Read the plan at docs/plans/p.md.")


if __name__ == "__main__":
    unittest.main()
