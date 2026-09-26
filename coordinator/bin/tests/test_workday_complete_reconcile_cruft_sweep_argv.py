from __future__ import annotations

import importlib.util
import io
import os
import sys
import unittest
from unittest import mock

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_SCRIPT_DIR)
_CLI = os.path.join(_BIN_DIR, "workday-complete-reconcile.py")


def _load_cli_module():
    spec = importlib.util.spec_from_file_location("_wcr_cruft_argv_under_test", _CLI)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class CruftSweepArgvTest(unittest.TestCase):

    def test_t1_shipped_py_default_routes_through_interpreter(self) -> None:
        mod = _load_cli_module()
        default_bin = mod._default_cruft_sweep_bin()
        self.assertEqual(os.path.splitext(default_bin)[1], ".py")
        for platform in ("nt", "posix"):
            with self.subTest(os_name=platform), mock.patch.object(mod.os, "name", platform):
                self.assertEqual(
                    mod._cruft_sweep_argv(default_bin), [sys.executable, default_bin]
                )

    def test_t2_extensionless_shim_routes_through_interpreter(self) -> None:
        mod = _load_cli_module()
        shim = os.path.join("settings-home", "bin", "cruft-sweep")
        for platform in ("nt", "posix"):
            with self.subTest(os_name=platform), mock.patch.object(mod.os, "name", platform):
                self.assertEqual(mod._cruft_sweep_argv(shim), [sys.executable, shim])

    def test_t3_cmd_sibling_stays_a_bare_launch(self) -> None:
        mod = _load_cli_module()
        cmd = os.path.join("bin", "cruft-sweep.cmd")
        for platform in ("nt", "posix"):
            with self.subTest(os_name=platform), mock.patch.object(mod.os, "name", platform):
                self.assertEqual(mod._cruft_sweep_argv(cmd), [cmd])


class RunCruftSweepDispatchTest(unittest.TestCase):

    def test_t4_routed_argv_reaches_subprocess_with_flags_intact(self) -> None:
        mod = _load_cli_module()
        captured: dict = {}

        def _fake_run(argv, **kwargs):
            captured["argv"] = argv
            return mock.Mock(returncode=0)

        with mock.patch.object(mod.subprocess, "run", _fake_run):
            rc = mod.run_cruft_sweep()

        self.assertEqual(rc, 0)
        argv = captured["argv"]
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(os.path.splitext(argv[1])[1], ".py")
        self.assertEqual(argv[2:], ["--class", "all", "--apply", "--quiet"])

    def test_t5_spawn_failure_stays_non_blocking(self) -> None:
        mod = _load_cli_module()

        def _raising_run(argv, **kwargs):
            raise OSError(8, "%1 is not a valid Win32 application")

        err = io.StringIO()
        with mock.patch.object(mod.subprocess, "run", _raising_run):
            rc = mod.run_cruft_sweep(err=err)

        self.assertEqual(rc, 0)
        self.assertIn("cruft-sweep Step 1.5 could not be invoked", err.getvalue())


if __name__ == "__main__":
    unittest.main()
