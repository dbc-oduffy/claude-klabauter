from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

import importlib

query_completion_rollups = importlib.import_module("query-completion-rollups")


_DAY_WEEK_RECORDS = [
    {"grain": "day", "period": "2026-08-15"},
    {"grain": "week", "period": "2026-W33"},
]


class TestHelp(unittest.TestCase):
    def test_help_states_since_window_is_observed_at_relative(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
            query_completion_rollups.main(["--help"])

        self.assertEqual(ctx.exception.code, 0)
        text = stdout.getvalue().lower()
        self.assertIn("observed_at", text)
        self.assertIn("30-day", text)

    def test_help_states_dedup_is_week_only(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit):
            query_completion_rollups.main(["--help"])

        text = stdout.getvalue().lower()
        self.assertIn("dedup", text)
        self.assertIn("week", text)
        self.assertIn("not deduped", text)

    def test_help_states_max_commit_sha_is_a_sample(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit):
            query_completion_rollups.main(["--help"])

        text = stdout.getvalue().lower()
        self.assertIn("lexicographically-greatest", text)
        self.assertIn("sample", text)

    def test_help_states_reviews_and_verdicts_count_valid_trail_records(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit):
            query_completion_rollups.main(["--help"])

        text = stdout.getvalue().lower()
        self.assertIn("reviews_conducted", text)
        self.assertIn("verdicts", text)
        self.assertIn("valid", text)

    def test_unrecognized_argument_exits_2(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            query_completion_rollups.main(["--bogus"])

        self.assertEqual(ctx.exception.code, 2)


class TestMain(unittest.TestCase):
    def test_day_and_week_records_on_success(self):
        with mock.patch.object(
            query_completion_rollups, "resolve_repo_root_or_exit", return_value="/repo/match"
        ), mock.patch.object(
            query_completion_rollups, "resolve_claude_klabauter_root_or_exit", return_value=os.getcwd()
        ), mock.patch(
            "coordinator_core.ops.emit.resolvers.resolve_context", return_value="fake-ctx"
        ), mock.patch(
            "coordinator_core.ops.emit.sections.rollups.collect",
            return_value=(_DAY_WEEK_RECORDS, []),
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = query_completion_rollups.main([])

        self.assertEqual(exit_code, 0)
        printed = json.loads(stdout.getvalue())
        self.assertEqual([r["grain"] for r in printed], ["day", "week"])

    def test_unresolvable_root_returns_1_without_calling_collect(self):
        with mock.patch.object(
            query_completion_rollups, "resolve_repo_root_or_exit", return_value=1
        ), mock.patch(
            "coordinator_core.ops.emit.sections.rollups.collect"
        ) as collect_mock:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = query_completion_rollups.main([])

        self.assertEqual(exit_code, 1)
        collect_mock.assert_not_called()

    def test_collect_failure_returns_1_with_diagnostic(self):
        with mock.patch.object(
            query_completion_rollups, "resolve_repo_root_or_exit", return_value="/repo/match"
        ), mock.patch.object(
            query_completion_rollups, "resolve_claude_klabauter_root_or_exit", return_value=os.getcwd()
        ), mock.patch(
            "coordinator_core.ops.emit.resolvers.resolve_context", return_value="fake-ctx"
        ), mock.patch(
            "coordinator_core.ops.emit.sections.rollups.collect",
            side_effect=RuntimeError("boom"),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = query_completion_rollups.main([])

        self.assertEqual(exit_code, 1)
        self.assertIn("boom", stderr.getvalue())

    def test_claude_klabauter_root_resolution_failure_returns_1_without_calling_collect(self):
        with mock.patch.object(
            query_completion_rollups, "resolve_repo_root_or_exit", return_value="/repo/match"
        ), mock.patch.object(
            query_completion_rollups, "resolve_claude_klabauter_root_or_exit", return_value=1
        ), mock.patch(
            "coordinator_core.ops.emit.sections.rollups.collect"
        ) as collect_mock:
            exit_code = query_completion_rollups.main([])

        self.assertEqual(exit_code, 1)
        collect_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
