"""A scaffolded plan neither offers nor inlines review_signals."""

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


class ReviewSignalsNotOfferedTest(unittest.TestCase):
    def test_template_does_not_offer_review_signals(self):
        self.assertNotIn("# review_signals", _scaffolded_plan_text())

    def test_template_does_not_inline_a_signal_vocabulary(self):
        inlined = [
            line
            for line in _scaffolded_plan_text().splitlines()
            if line.startswith("#   - ") and "positive claim" in line
        ]
        self.assertEqual(inlined, [], "signal vocabulary is referenced, not vendored")

    def test_review_signals_is_not_a_default_frontmatter_key(self):
        fm_text = _scaffolded_plan_text().split("---", 2)[1]
        fields = yaml.safe_load(fm_text)
        self.assertNotIn("review_signals", fields)


if __name__ == "__main__":
    unittest.main()
