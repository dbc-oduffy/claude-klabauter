from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

import yaml

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_review_signals_test",
        str(_BIN_DIR / "coordinator-doc-new.py"),
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_review_signals_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_MOD = _load_cli_module()


def _scaffolded_plan_text() -> str:
    return _MOD._scaffold_plan(
        title="Sample plan for review-signals template test",
        branch="main",
        author="test-author",
    )


class ReviewSignalsOfferedTest(unittest.TestCase):
    def test_optional_keys_block_offers_review_signals(self):
        self.assertIn("# review_signals:", _scaffolded_plan_text())

    def test_review_signals_points_at_the_contract_not_an_enum(self):
        text = _scaffolded_plan_text()
        self.assertIn("coordinator/contract/review-signals.json", text)
        offered = [
            line
            for line in text.splitlines()
            if line.startswith("#   - ") and "positive claim" in line
        ]
        self.assertEqual(
            len(offered),
            1,
            "expected a single sample signal row, not an inlined vocabulary",
        )

    def test_review_signals_stays_commented_out(self):
        fm_text = _scaffolded_plan_text().split("---", 2)[1]
        fields = yaml.safe_load(fm_text)
        self.assertNotIn("review_signals", fields)


if __name__ == "__main__":
    unittest.main()
