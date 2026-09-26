from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from coordinator_core.ops.handoff_author_lint import _handler, lint_text
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
        return _handler({"handoff_path": f"state/handoffs/{name}"}, root)


def _worktree(root: Path) -> Path:
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
        self.assertNotIn("LEDGER_ROW_UNPARSEABLE", _codes(_lint(_doc())))

    def test_absent_acceptance_criteria_section_is_not_a_finding(self):
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
        result = _lint(_doc(ac="- prose bullet"))
        hint = next(
            f["hint"] for f in result["findings"] if f["code"] == "AC_NO_CHECKBOXES"
        )
        self.assertIn("- [ ]", hint)

    def test_a_ticked_box_alone_still_counts_as_checkboxes(self):
        self.assertNotIn("AC_NO_CHECKBOXES", _codes(_lint(_doc(ac="- [x] done"))))


class SessionLedgerTest(unittest.TestCase):
    def test_a_duration_row_is_reported(self):
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
            result = _handler({}, _worktree(Path(tmp)))
        self.assertEqual(result["exit_code"], 2)
        self.assertFalse(result["clean"])

    def test_absent_repo_root_is_exit_2(self):
        result = _handler({"handoff_path": "state/handoffs/x.md"}, None)
        self.assertEqual(result["exit_code"], 2)

    def test_nonexistent_file_is_exit_2_not_clean(self):
        with tempfile.TemporaryDirectory(prefix="author-lint-") as tmp:
            result = _handler(
                    {"handoff_path": "state/handoffs/nope.md"}, _worktree(Path(tmp))
                )
        self.assertEqual(result["exit_code"], 2)
        self.assertFalse(result["clean"])


class LedgerGrammarOwnershipTest(unittest.TestCase):

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


class LintTextSeamTest(unittest.TestCase):

    def test_clean_body_is_clean(self):
        self.assertEqual(lint_text(_doc()), [])

    def test_summary_placeholder_code(self):
        placeholder = (
            "PLACEHOLDER — replace with one-line spinoff summary (≤140 chars)"
        )
        codes = [f["code"] for f in lint_text(_doc(summary=f'"{placeholder}"'))]
        self.assertIn("SUMMARY_PLACEHOLDER", codes)

    def test_summary_over_cap_code(self):
        codes = [f["code"] for f in lint_text(_doc(summary="x" * 200))]
        self.assertIn("SUMMARY_OVER_CAP", codes)

    def test_ac_no_checkboxes_code(self):
        codes = [f["code"] for f in lint_text(_doc(ac="- prose bullet"))]
        self.assertIn("AC_NO_CHECKBOXES", codes)

    def test_ledger_row_unparseable_code(self):
        rows = "2026-08-19 | abc123 | S | 0.3d / 0o | Wrote a duration\n"
        codes = [f["code"] for f in lint_text(_doc(ledger_rows=rows))]
        self.assertIn("LEDGER_ROW_UNPARSEABLE", codes)

    def test_body_with_no_frontmatter_still_runs_body_grammars(self):
        text = (
            "## Acceptance criteria\n\n"
            "- prose bullet\n\n"
            "## Session Ledger\n\n"
            "<!-- Format: YYYY-MM-DD | <sid6> | <tshirt> | <Nd / No> | <summary> -->\n"
            "2026-08-19 | abc123 | S | 0.3d / 0o | Wrote a duration\n"
        )
        codes = [f["code"] for f in lint_text(text)]
        self.assertIn("AC_NO_CHECKBOXES", codes)
        self.assertIn("LEDGER_ROW_UNPARSEABLE", codes)


if __name__ == "__main__":
    unittest.main()
