from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
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


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


def _started(run_dir, budget=10):
    return {
        "ok": True,
        "configured": True,
        "started": True,
        "key": "web",
        "variant": None,
        "run_dir": str(run_dir),
        "runner_pid": 1,
        "wait_budget_s": budget,
    }


def _run_validate(argv, route_side_effect, tmp_path, result=None):
    if result is not None:
        (Path(tmp_path) / "result.json").write_text(json.dumps(result), encoding="utf-8")
    clock = _Clock()
    with mock.patch.object(_cli_mod, "_sleep", clock.sleep), mock.patch.object(
        _cli_mod, "_monotonic", clock.monotonic
    ), mock.patch.object(
        _cli_mod.cc_invoke, "route", side_effect=route_side_effect
    ) as route_mock:
        code, out, err = _run(argv)
    return code, out, err, route_mock, clock


class TestValidateVerb(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = Path(self._tmp.name)

    def test_help_lists_validate(self):
        code, out, _ = _run(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("validate", out)

    def test_missing_key_exits_2(self):
        with mock.patch.object(_cli_mod.cc_invoke, "route") as route_mock:
            code, _, err = _run(["validate"])
        self.assertEqual(code, 2)
        self.assertIn("--key", err)
        route_mock.assert_not_called()

    def test_wait_non_integer_exits_2(self):
        with mock.patch.object(_cli_mod.cc_invoke, "route") as route_mock:
            code, _, _ = _run(["validate", "--key", "web", "--wait", "abc"])
        self.assertEqual(code, 2)
        route_mock.assert_not_called()

    def test_clean_result_exits_0_and_params_carry_variant_and_out(self):
        final = {"ok": True, "configured": True, "defect_count": 0}
        code, out, _, route_mock, _ = _run_validate(
            ["validate", "--key", "web", "--variant", "v1", "--out", "/o"],
            [_started(self.run_dir)],
            self.run_dir,
            final,
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), final)
        op, params, _ = route_mock.call_args.args[:3]
        self.assertEqual(op, "app_session.validate")
        self.assertEqual(
            params,
            {"repo_root": "/repo/root", "key": "web", "variant": "v1", "out_dir": "/o"},
        )

    def test_defects_exit_4(self):
        final = {"ok": True, "configured": True, "defect_count": 3}
        code, *_ = _run_validate(
            ["validate", "--key", "web"], [_started(self.run_dir)], self.run_dir, final
        )
        self.assertEqual(code, 4)

    def test_refusal_exits_5(self):
        refusal = {"ok": False, "configured": True, "stage": "config", "error": "x"}
        code, out, _, route_mock, _ = _run_validate(
            ["validate", "--key", "web"], [refusal], self.run_dir
        )
        self.assertEqual(code, 5)
        self.assertEqual(json.loads(out), refusal)
        route_mock.assert_called_once()

    def test_incomplete_result_exits_5(self):
        final = {"ok": False, "configured": True, "stage": "build", "error": "e"}
        code, *_ = _run_validate(
            ["validate", "--key", "web"], [_started(self.run_dir)], self.run_dir, final
        )
        self.assertEqual(code, 5)

    def test_not_configured_exits_0(self):
        nc = {"ok": True, "configured": False}
        code, out, *_ = _run_validate(["validate", "--key", "web"], [nc], self.run_dir)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), nc)

    def test_lapse_exits_5_and_tears_down(self):
        code, out, _, route_mock, _ = _run_validate(
            ["validate", "--key", "web"], [_started(self.run_dir, budget=3), {"ok": True}], self.run_dir
        )
        self.assertEqual(code, 5)
        parsed = json.loads(out)
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["stage"], "sweep")
        self.assertEqual(route_mock.call_count, 2)
        op, params, _ = route_mock.call_args.args[:3]
        self.assertEqual(op, "app_session.teardown")
        self.assertEqual(params["key"], "web@validate")

    def test_wait_recalls_op_on_guard_refusal(self):
        guard = {"ok": False, "configured": True, "stage": "guard", "error": "held"}
        final = {"ok": True, "configured": True, "defect_count": 0}
        code, _, _, route_mock, clock = _run_validate(
            ["validate", "--key", "web", "--wait", "30"],
            [guard, guard, _started(self.run_dir)],
            self.run_dir,
            final,
        )
        self.assertEqual(code, 0)
        self.assertEqual(route_mock.call_count, 3)
        self.assertEqual(clock.sleeps[:2], [2, 2])

    def test_guard_refusal_without_wait_is_not_retried(self):
        guard = {"ok": False, "configured": True, "stage": "guard", "error": "held"}
        code, _, _, route_mock, _ = _run_validate(
            ["validate", "--key", "web"], [guard], self.run_dir
        )
        self.assertEqual(code, 5)
        route_mock.assert_called_once()

    def test_wait_lapses_on_persistent_guard(self):
        guard = {"ok": False, "configured": True, "stage": "guard", "error": "held"}
        code, _, _, route_mock, _ = _run_validate(
            ["validate", "--key", "web", "--wait", "5"], [guard] * 10, self.run_dir
        )
        self.assertEqual(code, 5)
        self.assertEqual(route_mock.call_count, 3)


if __name__ == "__main__":
    unittest.main()
