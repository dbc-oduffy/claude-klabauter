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

query_completion_receipts = importlib.import_module("query-completion-receipts")


_RECEIPTS = [
    {"receipt_id": "r1", "repo": "x"},
    {"receipt_id": "r2", "repo": "x"},
]


class TestHelp(unittest.TestCase):
    def test_help_exits_0_and_names_superseded(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
            query_completion_receipts.main(["--help"])

        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("superseded", stdout.getvalue().lower())

    def test_unrecognized_argument_exits_2(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            query_completion_receipts.main(["--bogus"])

        self.assertEqual(ctx.exception.code, 2)


class TestMain(unittest.TestCase):
    def test_stdout_is_a_bare_list_of_receipts(self):
        with mock.patch.object(
            query_completion_receipts, "resolve_repo_root_or_exit", return_value="/repo/match"
        ), mock.patch.object(
            query_completion_receipts, "resolve_claude_klabauter_root_or_exit", return_value=os.getcwd()
        ), mock.patch(
            "coordinator_core.ops.emit.resolvers.resolve_context", return_value="fake-ctx"
        ), mock.patch(
            "coordinator_core.ops.emit.sections.completion_receipts.collect",
            return_value=(_RECEIPTS, [{"path": "p", "reason": "bad"}]),
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = query_completion_receipts.main([])

        self.assertEqual(exit_code, 0)
        printed = json.loads(stdout.getvalue())
        self.assertIsInstance(printed, list)
        self.assertEqual([r["receipt_id"] for r in printed], ["r1", "r2"])

    def test_unresolvable_root_returns_1_without_calling_collect(self):
        with mock.patch.object(
            query_completion_receipts, "resolve_repo_root_or_exit", return_value=1
        ), mock.patch(
            "coordinator_core.ops.emit.sections.completion_receipts.collect"
        ) as collect_mock:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = query_completion_receipts.main([])

        self.assertEqual(exit_code, 1)
        collect_mock.assert_not_called()

    def test_collect_failure_returns_1_with_diagnostic(self):
        with mock.patch.object(
            query_completion_receipts, "resolve_repo_root_or_exit", return_value="/repo/match"
        ), mock.patch.object(
            query_completion_receipts, "resolve_claude_klabauter_root_or_exit", return_value=os.getcwd()
        ), mock.patch(
            "coordinator_core.ops.emit.resolvers.resolve_context", return_value="fake-ctx"
        ), mock.patch(
            "coordinator_core.ops.emit.sections.completion_receipts.collect",
            side_effect=RuntimeError("boom"),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = query_completion_receipts.main([])

        self.assertEqual(exit_code, 1)
        self.assertIn("boom", stderr.getvalue())

    def test_claude_klabauter_root_resolution_failure_returns_1_without_calling_collect(self):
        with mock.patch.object(
            query_completion_receipts, "resolve_repo_root_or_exit", return_value="/repo/match"
        ), mock.patch.object(
            query_completion_receipts, "resolve_claude_klabauter_root_or_exit", return_value=1
        ), mock.patch(
            "coordinator_core.ops.emit.sections.completion_receipts.collect"
        ) as collect_mock:
            exit_code = query_completion_receipts.main([])

        self.assertEqual(exit_code, 1)
        collect_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
