"""test_query_commit_closures.py -- C4's own test surface for `coordinator/bin/
query-commit-closures.py`.

Spec backlink: plan `2026-08-22-the-commit-closure-pipe-carries-rows.md`
§ C4.

Pins, per C4's body: `--help`/`-h` exits 0 and states the three honesty
disclosures (landing-forward-only coverage, the 33.5%/931/2,779
going-forward figure, and the hand-authored-revert coverage limit); an
unrecognized argument exits 2; a successful run prints
`commit_closures.collect(ctx)`'s records list as parseable JSON on stdout
at exit 0; and a collect-side exception exits 1 with the
`query-commit-closures: ` stderr prefix, never a silent zero-rows exit 0.
`--help`/`-h` and an unrecognized argument both go through `argparse`,
which raises `SystemExit` (0 and 2 respectively) rather than returning --
matching `test_query_goals.py`'s convention.

AC10: a revert row (non-null `reverts_sha`) survives to stdout verbatim --
asserted explicitly here, not merely a close row, since a test covering
only close rows would pass while missing half the deliverable (cockpit's
assert-AND-retract framing).

`collect()` itself is stubbed throughout -- this suite never touches a
real commit ledger or spawns `git` (see `test_query_goals.py`'s own
stubbing pattern, mirrored here).
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

query_commit_closures = importlib.import_module("query-commit-closures")


_SAMPLE_RECORDS = [
    {
        "repo": "claude-klabauter",
        "coordinator_root_path": ".",
        "provenance": {"source": "local_fs", "path": "", "derivation": "parsed"},
        "item_id": "C4",
        "sha": "a" * 40,
        "reachable_on_default_branch": True,
        "reverts_sha": None,
    },
    {
        "repo": "claude-klabauter",
        "coordinator_root_path": ".",
        "provenance": {"source": "local_fs", "path": "", "derivation": "parsed"},
        "item_id": "C4",
        "sha": "b" * 40,
        "reachable_on_default_branch": True,
        "reverts_sha": "a" * 40,
    },
]


class TestHelp(unittest.TestCase):
    def test_help_states_honesty_disclosures(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
            query_commit_closures.main(["--help"])

        self.assertEqual(ctx.exception.code, 0)
        text = stdout.getvalue()
        self.assertIn("not backfilled", text)
        self.assertIn("33.5%", text)
        self.assertIn("931/2,779", text)
        self.assertIn("hand-authored revert", text)

    def test_unrecognized_argument_exits_2(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            query_commit_closures.main(["--bogus"])

        self.assertEqual(ctx.exception.code, 2)


class TestMain(unittest.TestCase):
    def test_records_printed_as_json_on_success(self):
        with mock.patch.object(
            query_commit_closures, "resolve_repo_root_or_exit", return_value="/repo/match"
        ), mock.patch.object(
            query_commit_closures, "resolve_claude_klabauter_root_or_exit", return_value=os.getcwd()
        ), mock.patch(
            "coordinator_core.ops.emit.resolvers.resolve_context", return_value="fake-ctx"
        ), mock.patch(
            "coordinator_core.ops.emit.sections.commit_closures.collect",
            return_value=(_SAMPLE_RECORDS, []),
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = query_commit_closures.main([])

        self.assertEqual(exit_code, 0)
        printed = json.loads(stdout.getvalue())
        self.assertEqual(printed, _SAMPLE_RECORDS)

    def test_revert_row_survives_to_stdout(self):
        """AC10 -- a revert row (non-null reverts_sha) is not dropped or
        reshaped; a test asserting only close rows would pass while
        missing half the deliverable."""
        with mock.patch.object(
            query_commit_closures, "resolve_repo_root_or_exit", return_value="/repo/match"
        ), mock.patch.object(
            query_commit_closures, "resolve_claude_klabauter_root_or_exit", return_value=os.getcwd()
        ), mock.patch(
            "coordinator_core.ops.emit.resolvers.resolve_context", return_value="fake-ctx"
        ), mock.patch(
            "coordinator_core.ops.emit.sections.commit_closures.collect",
            return_value=(_SAMPLE_RECORDS, []),
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = query_commit_closures.main([])

        self.assertEqual(exit_code, 0)
        printed = json.loads(stdout.getvalue())
        revert_rows = [r for r in printed if r["reverts_sha"] is not None]
        self.assertEqual(len(revert_rows), 1)
        self.assertEqual(revert_rows[0]["reverts_sha"], "a" * 40)
        self.assertEqual(revert_rows[0]["sha"], "b" * 40)

    def test_unresolvable_root_returns_1_without_calling_collect(self):
        with mock.patch.object(
            query_commit_closures, "resolve_repo_root_or_exit", return_value=1
        ), mock.patch(
            "coordinator_core.ops.emit.sections.commit_closures.collect"
        ) as collect_mock:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = query_commit_closures.main([])

        self.assertEqual(exit_code, 1)
        collect_mock.assert_not_called()

    def test_claude_klabauter_root_resolution_failure_returns_1_without_calling_collect(self):
        with mock.patch.object(
            query_commit_closures, "resolve_repo_root_or_exit", return_value="/repo/match"
        ), mock.patch.object(
            query_commit_closures, "resolve_claude_klabauter_root_or_exit", return_value=1
        ), mock.patch(
            "coordinator_core.ops.emit.sections.commit_closures.collect"
        ) as collect_mock:
            exit_code = query_commit_closures.main([])

        self.assertEqual(exit_code, 1)
        collect_mock.assert_not_called()

    def test_collect_failure_returns_1_with_diagnostic(self):
        with mock.patch.object(
            query_commit_closures, "resolve_repo_root_or_exit", return_value="/repo/match"
        ), mock.patch.object(
            query_commit_closures, "resolve_claude_klabauter_root_or_exit", return_value=os.getcwd()
        ), mock.patch(
            "coordinator_core.ops.emit.resolvers.resolve_context", return_value="fake-ctx"
        ), mock.patch(
            "coordinator_core.ops.emit.sections.commit_closures.collect",
            side_effect=RuntimeError("boom"),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = query_commit_closures.main([])

        self.assertEqual(exit_code, 1)
        self.assertIn("boom", stderr.getvalue())
        self.assertIn("query-commit-closures:", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
