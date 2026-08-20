"""test_coordinator_doc_new_review_signals_offered.py -- pins `review_signals`
into the plan scaffold's commented optional-keys block.

Purpose: `review_signals` is the field `/review` reads to route a reviewer;
when it is absent the routing degrades to prose-matching the routing table,
silently and far downstream of authoring. The field also carries a positive
claim by its absence ("no specialist and no external-docs surface is in
play"), which only holds if the author was offered the key and declined it --
a key never surfaced makes "absent" indistinguishable from "never offered".

Negative-spec: does NOT assert any warn, nag, or write-guard on an absent
`review_signals`. Converting a deliberate positive claim into a lint failure
would make every plan carry the field defensively and destroy the signal --
named here so a later reader does not add the check thinking it was the
obvious fix this omitted.

Negative-spec: does NOT inline the signal vocabulary. The ids are
single-sourced in DoE-claude's `coordinator/contract/review-signals.json`
(pinned there by its own parity test); the scaffold points at that contract
and this suite asserts the pointer, never an enum.

Spec backlink: cross-repo/inbox/2026-08-20-doe-claude-em-doc-new-omits-review-signals.md

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as test_coordinator_doc_new_predecessor.py.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_review_signals_offered.py -v
"""
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
        """The vocabulary is single-sourced in the plugin's contract file; the
        scaffold cites it rather than carrying a second copy of the ids."""
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
        """Offered, not injected -- an emitted key would turn every scaffolded
        plan into a defensive claim and destroy the absence semantics."""
        fm_text = _scaffolded_plan_text().split("---", 2)[1]
        fields = yaml.safe_load(fm_text)
        self.assertNotIn("review_signals", fields)


if __name__ == "__main__":
    unittest.main()
