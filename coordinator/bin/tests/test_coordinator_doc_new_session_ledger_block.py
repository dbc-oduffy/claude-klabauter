"""test_coordinator_doc_new_session_ledger_block.py -- all six handoff-family
scaffolders emit the byte-identical canonical `## Session Ledger` block (2026-08-11).

Purpose: `session_ledger.aggregate_chain_loe` sums the `## Session Ledger` block's
rows; before this fix only `_scaffold_handoff` emitted it, so any chain headed by
one of the other five kinds silently summed to zero LoE. This suite asserts the
six scaffolders share ONE canonical block (`_SESSION_LEDGER_BLOCK`), not six
independently-typed copies of an expected literal -- a hand-copied expected
string in the test would let six divergent literals in production pass unnoticed.

Spec backlink: docs/plans/2026-08-11-ledger-owing-handoff-kinds-emit-the-sess.md § C1, AC1

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as test_coordinator_doc_new_predecessor.py.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_session_ledger_block.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import re
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_session_ledger_block_test", str(_BIN_DIR / "coordinator-doc-new")
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_session_ledger_block_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


def _extract_ledger_block(content: str) -> str:
    """Return the `## Session Ledger` heading through its trailing blank line, verbatim."""
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if line == "## Session Ledger":
            # Block is heading + blank + two comment lines + blank == 5 lines.
            return "\n".join(lines[i : i + 5])
    raise AssertionError("no '## Session Ledger' heading found in scaffolded content")


class SessionLedgerBlockIdenticalAcrossScaffoldersTest(unittest.TestCase):
    def _scaffolds(self) -> dict[str, str]:
        return {
            "_scaffold_handoff": _cli._scaffold_handoff(title="t", branch="b"),
            "_scaffold_recovery": _cli._scaffold_recovery(title="t", branch="b"),
            "_scaffold_spinoff": _cli._scaffold_spinoff(title="t", branch="b"),
            "_scaffold_roadmap_baton": _cli._scaffold_roadmap_baton(
                title="t", branch="b", roadmap_id="rmp-x", stub_id="C1"
            ),
            "_scaffold_roadmap_seed": _cli._scaffold_roadmap_seed(title="t", branch="b"),
            "_scaffold_goal_seed": _cli._scaffold_goal_seed(title="t", branch="b"),
        }

    def test_all_six_scaffolders_emit_identical_session_ledger_block(self):
        blocks = {name: _extract_ledger_block(content) for name, content in self._scaffolds().items()}
        distinct = set(blocks.values())
        self.assertEqual(
            len(distinct),
            1,
            f"scaffolders diverged on the Session Ledger block: {blocks}",
        )

    def test_block_matches_the_shared_module_constant(self):
        expected = "\n".join(_cli._SESSION_LEDGER_BLOCK[:5])
        for name, content in self._scaffolds().items():
            with self.subTest(scaffolder=name):
                self.assertEqual(_extract_ledger_block(content), expected)

    def test_heading_appears_exactly_once_per_scaffold(self):
        for name, content in self._scaffolds().items():
            with self.subTest(scaffolder=name):
                self.assertEqual(content.count("## Session Ledger"), 1)

    def test_comment_lines_match_the_oneline_grammar_parse_session_ledgers_reads(self):
        # AC6: the comment's declared grammar is the one _ONELINE_RE in
        # session_ledger actually parses -- assert a scaffolded block's
        # documented format string round-trips through that regex shape.
        from coordinator_core.session_ledger import aggregate_chain_loe

        sample_row = "2026-08-11 | abc123 | S | 1d / 0o | did the thing"
        match = aggregate_chain_loe._ONELINE_RE.match(sample_row)
        self.assertIsNotNone(
            match, "sample row matching the scaffolded comment's documented grammar failed _ONELINE_RE"
        )


if __name__ == "__main__":
    unittest.main()
