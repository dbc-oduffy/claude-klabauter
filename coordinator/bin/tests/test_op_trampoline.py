"""test_op_trampoline.py -- C0's own test surface for `coordinator/bin/
lib/op_trampoline.py`.

Spec backlink: plan `2026-08-11-three-trampolines-and-the-bare-repo-producer.md`
§ C0.

Pins, per C0's body: `run()` returns 0 and prints the routed result as JSON
on a successful `cc_invoke.route_mutation` (stubbed); returns 1 and prints a
diagnostic naming `op_name` on a `RuntimeError` from `route_mutation`;
`resolve_repo_root_or_exit()` returns 1 with a diagnostic naming the cwd
when `resolve_checked_repo_root` returns `(None, ...)`; a MISMATCH verdict
is warned to stderr and the resolved root used anyway (DR-277).

C26 (plan `2026-08-20-a-refusal-cannot-exit-zero.md`): `run()` routes through
`cc_invoke.route_mutation`, not the bare `route` -- an in-envelope refusal
(`{"exit_code": N, ...}` or `{"failed": [...]}` or a nested `{"error": ...}`
with exit_code absent/0) raises `RouteMutationError` inside `route_mutation`
itself, which `run()` must catch and turn into exit code 1, never printing
the refusal payload as if it were a success and returning 0.

`resolve_claude_klabauter_root_or_exit(cli_name)` -- the third module-level helper,
extracted from the retired `query-file-attribution.py` (deleted 2026-08-23) and
`query-routine-signals.py`'s
hand-copied CLAUDE_KLABAUTER_ROOT-resolution sub-recipe -- returns the resolved root
and puts it on `sys.path` on success; returns 1 and prints a diagnostic
naming both `cli_name` and the underlying error on a `RuntimeError` from
`cc_invoke._resolve_claude_klabauter_root`.
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

import op_trampoline  # noqa: E402


def _verdict(verdict: str, resolved_root, sid="sess-x"):
    return {
        "verdict": verdict,
        "session_root": None,
        "resolved_root": resolved_root,
        "sid": sid,
        "message": f"repo-identity (checked resolver): fake {verdict} for test",
    }


class TestResolveRepoRootOrExit(unittest.TestCase):
    def test_match_returns_resolved_root_silently(self):
        with mock.patch.object(
            op_trampoline,
            "resolve_checked_repo_root",
            return_value=("/repo/match", _verdict("MATCH", "/repo/match")),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                root = op_trampoline.resolve_repo_root_or_exit()

        self.assertEqual(root, "/repo/match")
        self.assertEqual(stderr.getvalue(), "")

    def test_mismatch_warns_to_stderr_and_still_returns_resolved_root(self):
        v = _verdict("MISMATCH", "/repo/mismatch")
        with mock.patch.object(
            op_trampoline,
            "resolve_checked_repo_root",
            return_value=("/repo/mismatch", v),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                root = op_trampoline.resolve_repo_root_or_exit()

        self.assertEqual(root, "/repo/mismatch")
        self.assertIn(v["message"], stderr.getvalue())

    def test_unresolvable_root_returns_1_with_diagnostic_naming_cwd(self):
        with mock.patch.object(
            op_trampoline,
            "resolve_checked_repo_root",
            return_value=(None, _verdict("UNRESOLVED", None, sid=None)),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = op_trampoline.resolve_repo_root_or_exit()

        self.assertEqual(result, 1)
        self.assertIn(os.getcwd(), stderr.getvalue())


class TestResolveClaudeKlabauterRootOrExit(unittest.TestCase):
    def test_success_returns_root_and_updates_sys_path(self):
        fake_root = os.path.join(os.getcwd(), "fake-claude-klabauter-root")
        original_path = list(sys.path)
        try:
            with mock.patch.object(
                op_trampoline.cc_invoke, "_resolve_claude_klabauter_root", return_value=fake_root
            ):
                result = op_trampoline.resolve_claude_klabauter_root_or_exit("query-fake-cli")

            self.assertEqual(result, fake_root)
            self.assertIn(fake_root, sys.path)
        finally:
            sys.path[:] = original_path

    def test_success_does_not_duplicate_already_present_root(self):
        fake_root = os.path.join(os.getcwd(), "fake-claude-klabauter-root-2")
        original_path = list(sys.path)
        sys.path.insert(0, fake_root)
        try:
            with mock.patch.object(
                op_trampoline.cc_invoke, "_resolve_claude_klabauter_root", return_value=fake_root
            ):
                op_trampoline.resolve_claude_klabauter_root_or_exit("query-fake-cli")

            self.assertEqual(sys.path.count(fake_root), 1)
        finally:
            sys.path[:] = original_path

    def test_runtime_error_returns_1_with_diagnostic_naming_cli_and_error(self):
        with mock.patch.object(
            op_trampoline.cc_invoke,
            "_resolve_claude_klabauter_root",
            side_effect=RuntimeError("CLAUDE_KLABAUTER_ROOT unresolved"),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = op_trampoline.resolve_claude_klabauter_root_or_exit("query-fake-cli")

        self.assertEqual(result, 1)
        self.assertIn("query-fake-cli", stderr.getvalue())
        self.assertIn("CLAUDE_KLABAUTER_ROOT unresolved", stderr.getvalue())


class TestRun(unittest.TestCase):
    def _params_builder(self, argv):
        return {"argv": list(argv)}

    def test_success_returns_0_and_prints_routed_result_as_json(self):
        with mock.patch.object(
            op_trampoline,
            "resolve_checked_repo_root",
            return_value=("/repo/match", _verdict("MATCH", "/repo/match")),
        ), mock.patch.object(
            op_trampoline.cc_invoke, "route_mutation", return_value={"ok": True, "n": 3}
        ) as route_mock:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = op_trampoline.run(
                    "fake.op", self._params_builder, argv=["--x", "1"]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"ok": True, "n": 3})
        route_mock.assert_called_once()
        called_op, called_params, called_root = route_mock.call_args.args[:3]
        self.assertEqual(called_op, "fake.op")
        self.assertEqual(called_params, {"argv": ["--x", "1"]})
        self.assertEqual(called_root, "/repo/match")

    def test_route_runtime_error_returns_1_and_prints_diagnostic_naming_op(self):
        with mock.patch.object(
            op_trampoline,
            "resolve_checked_repo_root",
            return_value=("/repo/match", _verdict("MATCH", "/repo/match")),
        ), mock.patch.object(
            op_trampoline.cc_invoke,
            "route_mutation",
            side_effect=RuntimeError("transport down"),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = op_trampoline.run("fake.op", self._params_builder, argv=[])

        self.assertEqual(exit_code, 1)
        self.assertIn("fake.op", stderr.getvalue())
        self.assertIn("transport down", stderr.getvalue())

    def test_route_mutation_refusal_returns_1_not_0(self):
        """C26: an in-envelope refusal (exit_code!=0 inside a successfully-
        transported result) must not be printed and exited 0 as if it were a
        success payload. Red against pre-plan HEAD, where `run()` called the
        bare `route()` and never inspected the envelope at all."""
        refusal_result = {"exit_code": 1, "error": "op-level refusal"}
        with mock.patch.object(
            op_trampoline,
            "resolve_checked_repo_root",
            return_value=("/repo/match", _verdict("MATCH", "/repo/match")),
        ), mock.patch.object(
            op_trampoline.cc_invoke,
            "route_mutation",
            side_effect=op_trampoline.cc_invoke.RouteMutationError(
                "fake.op refused (exit_code=1)", refusal_result
            ),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = op_trampoline.run("fake.op", self._params_builder, argv=[])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("fake.op", stderr.getvalue())

    def test_unresolvable_root_short_circuits_to_1_without_calling_route(self):
        with mock.patch.object(
            op_trampoline,
            "resolve_checked_repo_root",
            return_value=(None, _verdict("UNRESOLVED", None, sid=None)),
        ), mock.patch.object(op_trampoline.cc_invoke, "route_mutation") as route_mock:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = op_trampoline.run("fake.op", self._params_builder, argv=[])

        self.assertEqual(exit_code, 1)
        route_mock.assert_not_called()

    def test_mismatch_warns_to_stderr_and_still_routes_successfully(self):
        v = _verdict("MISMATCH", "/repo/mismatch")
        with mock.patch.object(
            op_trampoline, "resolve_checked_repo_root", return_value=("/repo/mismatch", v)
        ), mock.patch.object(
            op_trampoline.cc_invoke, "route_mutation", return_value={"ok": True}
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = op_trampoline.run("fake.op", self._params_builder, argv=[])

        self.assertEqual(exit_code, 0)
        self.assertIn(v["message"], stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue()), {"ok": True})


if __name__ == "__main__":
    unittest.main()
