"""test_query_roadmap_serve.py -- C3's own test surface for `coordinator/bin/
query-roadmap-serve.py`.

Spec backlink: plan `2026-08-11-three-trampolines-and-the-bare-repo-producer.md`
§ C3.

Pins, per C3's body: `params_builder` parses `--roadmap-id <id>` into
`{"roadmap_id": <id>}`; a routed success prints the op's `roll_up` dict and
`critical_path` list (among the full payload) as JSON and returns 0; a
`RuntimeError` from `cc_invoke.route` (stubbed, as in
`test_op_trampoline.py`) prints a diagnostic naming the op and returns 1 --
not any other code.

AC7 (red-before-green): before this chunk, `coordinator/bin/query-roadmap-
serve.py` did not exist on disk, so no test importing it could have been
collected, let alone passed -- this file's own `import query_roadmap_serve`
at module scope is the honest red state pre-change (a collection error, not
a passing/failing assertion) and a clean pass post-change.
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
_LIB_DIR = os.path.join(_BIN_DIR, "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

import op_trampoline  # noqa: E402

sys.path.insert(0, _BIN_DIR)
import importlib.util as _ilu  # noqa: E402

_MODULE_PATH = os.path.join(_BIN_DIR, "query-roadmap-serve.py")
_spec = _ilu.spec_from_file_location("query_roadmap_serve", _MODULE_PATH)
query_roadmap_serve = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(query_roadmap_serve)


def _verdict(verdict: str, resolved_root, sid="sess-x"):
    return {
        "verdict": verdict,
        "session_root": None,
        "resolved_root": resolved_root,
        "sid": sid,
        "message": f"repo-identity (checked resolver): fake {verdict} for test",
    }


class TestParamsBuilder(unittest.TestCase):
    def test_parses_roadmap_id_flag(self):
        params = query_roadmap_serve.params_builder(["--roadmap-id", "rm-1"])
        self.assertEqual(params, {"roadmap_id": "rm-1"})

    def test_missing_value_exits_2(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            query_roadmap_serve.params_builder(["--roadmap-id"])
        self.assertEqual(ctx.exception.code, 2)

    def test_missing_flag_entirely_exits_2(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            query_roadmap_serve.params_builder([])
        self.assertEqual(ctx.exception.code, 2)

    def test_unrecognized_argument_exits_2(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            query_roadmap_serve.params_builder(["--roadmap-id", "rm-1", "--bogus"])
        self.assertEqual(ctx.exception.code, 2)

    def test_help_exits_0_and_prints_usage(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
            query_roadmap_serve.params_builder(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("usage:", stdout.getvalue())


class TestMainRouting(unittest.TestCase):
    _FAKE_PAYLOAD = {
        "roadmap_id": "rm-1",
        "nodes": [],
        "edges": [],
        "roll_up": {"total": 0, "by_status": {}, "pct_shipped": None},
        "critical_path": [],
        "scan_incomplete": False,
        "scan_errors": [],
    }

    def test_success_prints_roll_up_and_critical_path_and_returns_0(self):
        with mock.patch.object(
            op_trampoline,
            "resolve_checked_repo_root",
            return_value=("/repo/match", _verdict("MATCH", "/repo/match")),
        ), mock.patch.object(
            op_trampoline.cc_invoke, "route", return_value=self._FAKE_PAYLOAD
        ) as route_mock:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = op_trampoline.run(
                    "roadmap.serve",
                    query_roadmap_serve.params_builder,
                    argv=["--roadmap-id", "rm-1"],
                )

        self.assertEqual(exit_code, 0)
        printed = json.loads(stdout.getvalue())
        self.assertEqual(printed["roll_up"], self._FAKE_PAYLOAD["roll_up"])
        self.assertEqual(printed["critical_path"], self._FAKE_PAYLOAD["critical_path"])
        route_mock.assert_called_once()
        called_op, called_params, _called_root = route_mock.call_args.args[:3]
        self.assertEqual(called_op, "roadmap.serve")
        self.assertEqual(called_params, {"roadmap_id": "rm-1"})

    def test_route_runtime_error_exits_1_with_diagnostic_naming_op(self):
        with mock.patch.object(
            op_trampoline,
            "resolve_checked_repo_root",
            return_value=("/repo/match", _verdict("MATCH", "/repo/match")),
        ), mock.patch.object(
            op_trampoline.cc_invoke,
            "route",
            side_effect=RuntimeError("transport down"),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = op_trampoline.run(
                    "roadmap.serve",
                    query_roadmap_serve.params_builder,
                    argv=["--roadmap-id", "rm-1"],
                )

        self.assertEqual(exit_code, 1)
        self.assertIn("roadmap.serve", stderr.getvalue())
        self.assertIn("transport down", stderr.getvalue())

    def test_unresolvable_root_exits_1_without_calling_route(self):
        with mock.patch.object(
            op_trampoline,
            "resolve_checked_repo_root",
            return_value=(None, _verdict("UNRESOLVED", None, sid=None)),
        ), mock.patch.object(op_trampoline.cc_invoke, "route") as route_mock:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = op_trampoline.run(
                    "roadmap.serve",
                    query_roadmap_serve.params_builder,
                    argv=["--roadmap-id", "rm-1"],
                )

        self.assertEqual(exit_code, 1)
        route_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
