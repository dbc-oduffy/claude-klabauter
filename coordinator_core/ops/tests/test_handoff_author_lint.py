"""test_handoff_author_lint.py — `handoff.author_lint`, the author-time catch
for hand-typed values whose real gate fires much later, on someone else.

Covers the four finding codes and, as importantly, the four NON-findings — a
lint that fires on a freshly scaffolded, correctly empty body would be a new
gate rather than an earlier one, and the author would learn to ignore it.

Discharges AC-4 and AC-5 of
`state/handoffs/2026-08-21-handoffs-and-spinoffs-minimal-for-hand-rolling.md`.

FAST TIER: pure text in, findings out. Every case builds a file in a tmpdir;
no git spawn, no engine socket, no live corpus.

Run:
    python3 -m pytest coordinator_core/ops/tests/test_handoff_author_lint.py -v
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from coordinator_core.ops.handoff_author_lint import _handler
from coordinator_core.session_ledger.aggregate_chain_loe import unparseable_ledger_rows

_LEDGER_HEADING = "## Session Ledger"


def _doc(summary: str = "a real one-line summary", ac: str = "- [ ] do the thing",
         ledger_rows: str = "") -> str:
    return (
        "---\n"
        "title: t\n"
        f"summary: {summary}\n"
        "kind: spinoff\n"
        "---\n"
        "\n"
        "## Acceptance criteria\n"
        "\n"
        f"{ac}\n"
        "\n"
        f"{_LEDGER_HEADING}\n"
        "\n"
        "<!-- Format: YYYY-MM-DD | <sid6> | <tshirt> | <Nd / No> | <summary> -->\n"
        f"{ledger_rows}"
    )


def _lint(text: str, *, name: str = "2026-08-21-x.md") -> dict:
    with tempfile.TemporaryDirectory(prefix="author-lint-") as tmp:
        root = _worktree(Path(tmp))
        (root / "state" / "handoffs").mkdir(parents=True)
        (root / "state" / "handoffs" / name).write_text(text, encoding="utf-8")
        return asyncio.run(
            _handler({"handoff_path": f"state/handoffs/{name}"}, root)
        )


def _worktree(root: Path) -> Path:
    """`main_worktree_root` refuses to guess: it accepts a git common dir or a
    directory carrying a `.git` entry. A bare tmpdir is neither, so every case
    here plants an empty `.git` marker — no `git init`, no spawn."""
    (root / ".git").mkdir(exist_ok=True)
    return root


def _codes(result: dict) -> list[str]:
    return [f["code"] for f in result["findings"]]


class CleanBodyTest(unittest.TestCase):
    def test_a_well_formed_body_is_clean(self):
        result = _lint(_doc())
        self.assertEqual(result["exit_code"], 0, result["findings"])
        self.assertTrue(result["clean"])

    def test_a_freshly_scaffolded_empty_ledger_is_not_a_finding(self):
        """A handoff is born with an empty ledger block — the row is appended
        at `/handoff` or `/workstream-complete`. Firing here would train the
        author to ignore the lint."""
        self.assertNotIn("LEDGER_ROW_UNPARSEABLE", _codes(_lint(_doc())))

    def test_absent_acceptance_criteria_section_is_not_a_finding(self):
        """Not every handoff kind owns an AC section; requiring one would be a
        NEW gate, which this op's negative-spec forbids."""
        text = "---\ntitle: t\nsummary: s\n---\n\n## What this covers\n\nprose\n"
        self.assertEqual(_codes(_lint(text)), [])

    def test_a_valid_ledger_row_is_not_a_finding(self):
        rows = "2026-08-21 | 5f04d5 | M | 3d / 1o | Did the work\n"
        self.assertNotIn(
            "LEDGER_ROW_UNPARSEABLE", _codes(_lint(_doc(ledger_rows=rows)))
        )


class AcceptanceCriteriaTest(unittest.TestCase):
    def test_prose_bullets_under_the_heading_are_reported(self):
        result = _lint(_doc(ac="- do the thing\n- do the other thing"))
        self.assertIn("AC_NO_CHECKBOXES", _codes(result))
        self.assertEqual(result["exit_code"], 1)

    def test_the_hint_names_the_fix(self):
        """`docs/wiki/guard-messaging.md` § Register — a finding that does not
        name the alternative leaves the author exactly where the silent gate
        did."""
        result = _lint(_doc(ac="- prose bullet"))
        hint = next(
            f["hint"] for f in result["findings"] if f["code"] == "AC_NO_CHECKBOXES"
        )
        self.assertIn("- [ ]", hint)

    def test_a_ticked_box_alone_still_counts_as_checkboxes(self):
        self.assertNotIn("AC_NO_CHECKBOXES", _codes(_lint(_doc(ac="- [x] done"))))


class SessionLedgerTest(unittest.TestCase):
    def test_a_duration_row_is_reported(self):
        """The 2026-08-19 production instance verbatim: `0.3d` against a `\\d+`
        COUNT field. `aggregate` drops it silently and the chain sums to zero."""
        rows = "2026-08-19 | abc123 | S | 0.3d / 0o | Wrote a duration\n"
        result = _lint(_doc(ledger_rows=rows))
        self.assertIn("LEDGER_ROW_UNPARSEABLE", _codes(result))

    def test_the_reported_row_carries_its_line_number_and_text(self):
        rows = "2026-08-19 | abc123 | S | 0.3d / 0o | Wrote a duration\n"
        finding = next(
            f
            for f in _lint(_doc(ledger_rows=rows))["findings"]
            if f["code"] == "LEDGER_ROW_UNPARSEABLE"
        )
        self.assertIn("line ", finding["where"])
        self.assertIn("0.3d", finding["error"])

    def test_rows_after_the_next_heading_are_out_of_the_block(self):
        text = _doc() + "\n## Anti-scope\n\n- not a ledger row at all\n"
        self.assertNotIn("LEDGER_ROW_UNPARSEABLE", _codes(_lint(text)))


class SummaryTest(unittest.TestCase):
    def test_over_cap_summary_is_reported_as_advisory_truncation(self):
        result = _lint(_doc(summary="x" * 200))
        finding = next(
            f for f in result["findings"] if f["code"] == "SUMMARY_OVER_CAP"
        )
        self.assertIn("truncated", finding["error"])

    def test_placeholder_summary_is_reported(self):
        placeholder = (
            "PLACEHOLDER — replace with one-line spinoff summary (≤140 chars)"
        )
        self.assertIn(
            "SUMMARY_PLACEHOLDER", _codes(_lint(_doc(summary=f'"{placeholder}"')))
        )

    def test_at_cap_summary_is_clean(self):
        self.assertNotIn("SUMMARY_OVER_CAP", _codes(_lint(_doc(summary="x" * 140))))


class RefusalTest(unittest.TestCase):
    """Exit 2 is INDETERMINATE, never conflated with clean — a lint that
    reported a missing file as `clean: true` would be worse than no lint."""

    def test_missing_path_param_is_exit_2(self):
        with tempfile.TemporaryDirectory(prefix="author-lint-") as tmp:
            result = asyncio.run(_handler({}, _worktree(Path(tmp))))
        self.assertEqual(result["exit_code"], 2)
        self.assertFalse(result["clean"])

    def test_absent_repo_root_is_exit_2(self):
        result = asyncio.run(_handler({"handoff_path": "state/handoffs/x.md"}, None))
        self.assertEqual(result["exit_code"], 2)

    def test_nonexistent_file_is_exit_2_not_clean(self):
        with tempfile.TemporaryDirectory(prefix="author-lint-") as tmp:
            result = asyncio.run(
                _handler(
                    {"handoff_path": "state/handoffs/nope.md"}, _worktree(Path(tmp))
                )
            )
        self.assertEqual(result["exit_code"], 2)
        self.assertFalse(result["clean"])


class LedgerGrammarOwnershipTest(unittest.TestCase):
    """`unparseable_ledger_rows` lives beside the parser that defines what gets
    summed, so emitter, parser, and this lint cannot drift into three
    grammars."""

    def test_comments_blanks_and_table_rows_are_not_rejections(self):
        text = (
            "## Session Ledger\n"
            "\n"
            "<!-- Format: ... -->\n"
            "| Field | Value |\n"
            "-----\n"
        )
        self.assertEqual(unparseable_ledger_rows(text), [])

    def test_content_outside_a_ledger_block_is_never_examined(self):
        self.assertEqual(unparseable_ledger_rows("## Anti-scope\n\n0.3d junk\n"), [])


if __name__ == "__main__":
    unittest.main()
