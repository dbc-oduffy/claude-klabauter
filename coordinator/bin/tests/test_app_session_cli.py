"""test_app_session_cli.py — coverage for coordinator/bin/app-session.py.

Purpose: `app-session.py` is a bareword CLI trampoline over the three
`app_session.*` ops (see that file's own module docstring). This test
exercises the trampoline layer only — verb routing, argv validation, and
exit-code mapping — via a mocked `cc_invoke.route`, mirroring the loader
pattern `test_lesson_add.py` already uses for a hyphenated `coordinator/bin`
entrypoint with no importable module name.

Spec backlink: cross-repo/inbox/2026-08-15-*-app-session-ops-need-a-cli-entrypoint.md
Spec backlink: coordinator/bin/app-session.py

Run: python3 -m pytest coordinator/bin/tests/test_app_session_cli.py
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_CLI_PATH = _BIN_DIR / "app-session.py"

_loader = importlib.machinery.SourceFileLoader("app_session_cli", str(_CLI_PATH))
_spec = importlib.util.spec_from_loader("app_session_cli", _loader)
_cli_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_loader.exec_module(_cli_mod)


def _run(argv, repo_root="/repo/root"):
    with mock.patch.object(_cli_mod, "_resolve_repo_root", return_value=repo_root):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = _cli_mod.main(argv)
    return exit_code, stdout.getvalue(), stderr.getvalue()


class TestVerbRouting(unittest.TestCase):
    def test_launch_routes_to_app_session_launch(self):
        with mock.patch.object(
            _cli_mod.cc_invoke, "route", return_value={"ok": True, "configured": True}
        ) as route_mock:
            exit_code, stdout, _ = _run(["launch", "--key", "desktop"])

        self.assertEqual(exit_code, 0)
        route_mock.assert_called_once()
        op, params, root = route_mock.call_args.args[:3]
        self.assertEqual(op, "app_session.launch")
        self.assertEqual(params, {"repo_root": "/repo/root", "key": "desktop"})
        self.assertEqual(root, "/repo/root")
        self.assertEqual(
            __import__("json").loads(stdout), {"configured": True, "ok": True}
        )

    def test_census_routes_to_app_session_census(self):
        with mock.patch.object(
            _cli_mod.cc_invoke, "route", return_value={"ok": True, "sessions": []}
        ) as route_mock:
            exit_code, _, _ = _run(["census"])

        self.assertEqual(exit_code, 0)
        op, params, _ = route_mock.call_args.args[:3]
        self.assertEqual(op, "app_session.census")
        self.assertEqual(params, {"repo_root": "/repo/root"})

    def test_census_with_key_forwards_key(self):
        with mock.patch.object(
            _cli_mod.cc_invoke, "route", return_value={"ok": True, "sessions": []}
        ) as route_mock:
            exit_code, _, _ = _run(["census", "--key", "desktop"])

        self.assertEqual(exit_code, 0)
        _, params, _ = route_mock.call_args.args[:3]
        self.assertEqual(params, {"repo_root": "/repo/root", "key": "desktop"})

    def test_teardown_routes_to_app_session_teardown(self):
        with mock.patch.object(
            _cli_mod.cc_invoke, "route", return_value={"ok": True, "reaped": True}
        ) as route_mock:
            exit_code, _, _ = _run(["teardown", "--key", "desktop"])

        self.assertEqual(exit_code, 0)
        op, params, _ = route_mock.call_args.args[:3]
        self.assertEqual(op, "app_session.teardown")
        self.assertEqual(params, {"repo_root": "/repo/root", "key": "desktop"})


class TestUsageErrors(unittest.TestCase):
    def test_missing_verb_returns_usage_fail(self):
        exit_code, _, stderr = _run([])
        self.assertEqual(exit_code, 2)
        self.assertIn("usage", stderr)

    def test_unrecognized_verb_returns_usage_fail(self):
        exit_code, _, stderr = _run(["frobnicate"])
        self.assertEqual(exit_code, 2)
        self.assertIn("frobnicate", stderr)

    def test_launch_missing_key_returns_usage_fail(self):
        with mock.patch.object(_cli_mod.cc_invoke, "route") as route_mock:
            exit_code, _, stderr = _run(["launch"])

        self.assertEqual(exit_code, 2)
        self.assertIn("--key", stderr)
        route_mock.assert_not_called()

    def test_teardown_missing_key_returns_usage_fail(self):
        with mock.patch.object(_cli_mod.cc_invoke, "route") as route_mock:
            exit_code, _, stderr = _run(["teardown"])

        self.assertEqual(exit_code, 2)
        route_mock.assert_not_called()

    def test_unresolvable_repo_root_returns_usage_fail(self):
        with mock.patch.object(_cli_mod, "_resolve_repo_root", return_value=None), mock.patch.object(
            _cli_mod.cc_invoke, "route"
        ) as route_mock:
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = _cli_mod.main(["census"])

        self.assertEqual(exit_code, 2)
        route_mock.assert_not_called()


class TestNotConfiguredIsExitZero(unittest.TestCase):
    def test_not_configured_result_exits_zero(self):
        not_configured = {
            "ok": True,
            "configured": False,
            "op": "app_session.census",
            "key": "desktop",
            "reason": "no persisted handle for key 'desktop'",
        }
        with mock.patch.object(_cli_mod.cc_invoke, "route", return_value=not_configured):
            exit_code, stdout, _ = _run(["census", "--key", "desktop"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(__import__("json").loads(stdout), not_configured)


class TestTransportFailure(unittest.TestCase):
    def test_route_runtime_error_returns_transport_fail(self):
        with mock.patch.object(
            _cli_mod.cc_invoke, "route", side_effect=RuntimeError("transport down")
        ):
            exit_code, _, stderr = _run(["census"])

        self.assertEqual(exit_code, 3)
        self.assertIn("transport down", stderr)


if __name__ == "__main__":
    unittest.main()
