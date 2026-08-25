"""test_query_goals.py -- C1's own test surface for `coordinator/bin/
query-goals.py`.

Spec backlink: plan `2026-08-15-two-more-porter-trampolines-query-goals.md`
§ C1.

Pins, per C1's body: `--help`/`-h` exits 0 and states the
`key_results_status` absent-when-absent disclosure; an unrecognized
argument exits 2; a successful run prints `goals.collect(ctx)`'s records
list as parseable JSON on stdout at exit 0; and a collect-side exception
-- including `GoalsStateRootUnreadable` -- exits 1 with the `query-goals: `
stderr prefix, never a silent zero-goals exit 0. `--help`/`-h` and an
unrecognized argument both go through `argparse`, which raises
`SystemExit` (0 and 2 respectively) rather than returning -- matching
`test_query_routine_signals.py`'s convention.

`collect()` itself is stubbed throughout -- this suite never touches a
real `central_state_root` or the wire-read glob/parse machinery (see
`test_query_routine_signals.py`'s own stubbing pattern, mirrored here).
"""
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

query_goals = importlib.import_module("query-goals")


_SAMPLE_RECORDS = [
    {"goal_id": "abc123", "period": "week", "period_value": "2026-W33", "text": "ship it"},
    {"goal_id": "def456", "period": "day", "period_value": "2026-08-15", "text": "fix bug"},
]


class TestHelp(unittest.TestCase):
    def test_help_states_key_results_status_absent_when_absent(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
            query_goals.main(["--help"])

        self.assertEqual(ctx.exception.code, 0)
        text = stdout.getvalue()
        self.assertIn("key_results_status", text)
        self.assertIn("absent", text.lower())

    def test_unrecognized_argument_exits_2(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            query_goals.main(["--bogus"])

        self.assertEqual(ctx.exception.code, 2)


class TestMain(unittest.TestCase):
    def test_records_printed_as_json_on_success(self):
        with mock.patch.object(
            query_goals, "resolve_repo_root_or_exit", return_value="/repo/match"
        ), mock.patch.object(
            query_goals, "resolve_claude_klabauter_root_or_exit", return_value=os.getcwd()
        ), mock.patch(
            "coordinator_core.ops.emit.resolvers.resolve_context", return_value="fake-ctx"
        ), mock.patch(
            "coordinator_core.ops.emit.sections.goals.collect",
            return_value=(_SAMPLE_RECORDS, []),
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = query_goals.main([])

        self.assertEqual(exit_code, 0)
        printed = json.loads(stdout.getvalue())
        self.assertEqual(printed, _SAMPLE_RECORDS)

    def test_unresolvable_root_returns_1_without_calling_collect(self):
        with mock.patch.object(
            query_goals, "resolve_repo_root_or_exit", return_value=1
        ), mock.patch(
            "coordinator_core.ops.emit.sections.goals.collect"
        ) as collect_mock:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = query_goals.main([])

        self.assertEqual(exit_code, 1)
        collect_mock.assert_not_called()

    def test_claude_klabauter_root_resolution_failure_returns_1_without_calling_collect(self):
        with mock.patch.object(
            query_goals, "resolve_repo_root_or_exit", return_value="/repo/match"
        ), mock.patch.object(
            query_goals, "resolve_claude_klabauter_root_or_exit", return_value=1
        ), mock.patch(
            "coordinator_core.ops.emit.sections.goals.collect"
        ) as collect_mock:
            exit_code = query_goals.main([])

        self.assertEqual(exit_code, 1)
        collect_mock.assert_not_called()

    def test_collect_failure_returns_1_with_diagnostic(self):
        with mock.patch.object(
            query_goals, "resolve_repo_root_or_exit", return_value="/repo/match"
        ), mock.patch.object(
            query_goals, "resolve_claude_klabauter_root_or_exit", return_value=os.getcwd()
        ), mock.patch(
            "coordinator_core.ops.emit.resolvers.resolve_context", return_value="fake-ctx"
        ), mock.patch(
            "coordinator_core.ops.emit.sections.goals.collect",
            side_effect=RuntimeError("boom"),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = query_goals.main([])

        self.assertEqual(exit_code, 1)
        self.assertIn("boom", stderr.getvalue())
        self.assertIn("query-goals:", stderr.getvalue())

    def test_goals_state_root_unreadable_exits_1_not_0(self):
        from coordinator_core.ops.emit.sections.goals import GoalsStateRootUnreadable

        with mock.patch.object(
            query_goals, "resolve_repo_root_or_exit", return_value="/repo/match"
        ), mock.patch.object(
            query_goals, "resolve_claude_klabauter_root_or_exit", return_value=os.getcwd()
        ), mock.patch(
            "coordinator_core.ops.emit.resolvers.resolve_context", return_value="fake-ctx"
        ), mock.patch(
            "coordinator_core.ops.emit.sections.goals.collect",
            side_effect=GoalsStateRootUnreadable("/some/root: Permission denied"),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = query_goals.main([])

        self.assertEqual(exit_code, 1)
        self.assertIn("Permission denied", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
