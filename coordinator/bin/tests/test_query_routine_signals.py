"""test_query_routine_signals.py -- C4's own test surface for `coordinator/bin/
query-routine-signals.py`.

Spec backlink: plan `2026-08-11-three-trampolines-and-the-bare-repo-producer.md`
§ C4.

Pins, per C4's body: the six signal names in the emitter's own fixed order
(weekly, docs, arch-audit, bug-sweep, dormant-repo, distill-backlog); exit 1
with a diagnostic on repo-root-resolution failure and on a `collect()`
failure; and the two `--help` honesty strings (dormant-repo is a hardcoded
placeholder; `collect()` is not cheap). `--help`/`-h` and an unrecognized
argument both go through `argparse`, which raises `SystemExit` (0 and 2
respectively) rather than returning -- matching `query-roadmap-serve.py`'s
convention.

`collect()` itself is stubbed throughout -- it spawns real subprocesses and
60s-timeout git calls, which this suite must never invoke live (see
`test_op_trampoline.py`'s own stubbing pattern, mirrored here).

AC8 (red-before-green): before this chunk, `coordinator/bin/query-routine-
signals.py` did not exist, so every test in this file failed on
`ModuleNotFoundError`/import error against the pre-change tree. They pass
now that the file exists and behaves as pinned.
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

query_routine_signals = importlib.import_module("query-routine-signals")


_SIX_SIGNALS = [
    {"kind": "weekly"},
    {"kind": "docs"},
    {"kind": "arch-audit"},
    {"kind": "bug-sweep"},
    {"kind": "dormant-repo"},
    {"kind": "distill-backlog"},
]


class TestHelp(unittest.TestCase):
    def test_help_states_dormant_repo_is_hardcoded_placeholder(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
            query_routine_signals.main(["--help"])

        self.assertEqual(ctx.exception.code, 0)
        text = stdout.getvalue()
        self.assertIn("dormant-repo", text)
        self.assertIn("hardcoded", text.lower())
        self.assertIn("placeholder", text.lower())

    def test_help_states_collect_is_not_cheap(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit):
            query_routine_signals.main(["--help"])

        text = stdout.getvalue().lower()
        self.assertIn("not cheap", text)

    def test_unrecognized_argument_exits_2(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            query_routine_signals.main(["--bogus"])

        self.assertEqual(ctx.exception.code, 2)


class TestMain(unittest.TestCase):
    def test_six_signals_in_emitter_order_on_success(self):
        with mock.patch.object(
            query_routine_signals, "resolve_repo_root_or_exit", return_value="/repo/match"
        ), mock.patch.object(
            query_routine_signals, "resolve_claude_klabauter_root_or_exit", return_value=os.getcwd()
        ), mock.patch(
            "coordinator_core.ops.emit.resolvers.resolve_context", return_value="fake-ctx"
        ), mock.patch(
            "coordinator_core.ops.emit.sections.routine_signals.collect",
            return_value=(_SIX_SIGNALS, []),
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = query_routine_signals.main([])

        self.assertEqual(exit_code, 0)
        printed = json.loads(stdout.getvalue())
        self.assertEqual(
            [s["kind"] for s in printed],
            ["weekly", "docs", "arch-audit", "bug-sweep", "dormant-repo", "distill-backlog"],
        )

    def test_unresolvable_root_returns_1_without_calling_collect(self):
        with mock.patch.object(
            query_routine_signals, "resolve_repo_root_or_exit", return_value=1
        ), mock.patch(
            "coordinator_core.ops.emit.sections.routine_signals.collect"
        ) as collect_mock:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = query_routine_signals.main([])

        self.assertEqual(exit_code, 1)
        collect_mock.assert_not_called()

    def test_collect_failure_returns_1_with_diagnostic(self):
        with mock.patch.object(
            query_routine_signals, "resolve_repo_root_or_exit", return_value="/repo/match"
        ), mock.patch.object(
            query_routine_signals, "resolve_claude_klabauter_root_or_exit", return_value=os.getcwd()
        ), mock.patch(
            "coordinator_core.ops.emit.resolvers.resolve_context", return_value="fake-ctx"
        ), mock.patch(
            "coordinator_core.ops.emit.sections.routine_signals.collect",
            side_effect=RuntimeError("boom"),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = query_routine_signals.main([])

        self.assertEqual(exit_code, 1)
        self.assertIn("boom", stderr.getvalue())

    def test_claude_klabauter_root_resolution_failure_returns_1_without_calling_collect(self):
        # resolve_claude_klabauter_root_or_exit() itself never raises -- it catches
        # RuntimeError internally and returns 1 (see test_op_trampoline.py's
        # own coverage of that path). This CLI only needs to propagate the
        # int short-circuit without calling collect().
        with mock.patch.object(
            query_routine_signals, "resolve_repo_root_or_exit", return_value="/repo/match"
        ), mock.patch.object(
            query_routine_signals, "resolve_claude_klabauter_root_or_exit", return_value=1
        ), mock.patch(
            "coordinator_core.ops.emit.sections.routine_signals.collect"
        ) as collect_mock:
            exit_code = query_routine_signals.main([])

        self.assertEqual(exit_code, 1)
        collect_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
