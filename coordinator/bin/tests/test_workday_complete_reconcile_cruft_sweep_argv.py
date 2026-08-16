"""bin/tests/test_workday_complete_reconcile_cruft_sweep_argv.py

Purpose: Guard `workday-complete-reconcile.py`'s `_cruft_sweep_argv` launch-rung
selection -- the Step 1.5 cruft-sweep dispatch. No test covered this function
before 2026-08-16, which is why a narrowed extension/platform test shipped and
broke Step 1.5 on Windows silently: `run_cruft_sweep` catches OSError and always
returns 0 (non-blocking by design), so the failure surfaced only as an advisory
WARN line inside a 1.6MB ceremony transcript. The sweep never executed.

Negative-spec these tests pin:
  - the extension test is NOT "extensionless only" -- the shipped default from
    `_default_cruft_sweep_bin` is `cruft-sweep.py`, which must route through the
    interpreter, not launch bare
  - the platform test is NOT `os.name == "nt"` only -- a source file with no exec
    bit fails a bare launch on POSIX too

Mirrors the launch rung `wsc-session-disposition.py`'s `_session_claim_cli_argv`
already implements, which cites `_cruft_sweep_argv` as its precedent.

Test coverage:
  T1  the shipped default (`.py`) routes through sys.executable on every platform
  T2  an extensionless installed shim routes through sys.executable
  T3  a `.cmd` sibling stays a bare launch (directly executable)
  T4  `run_cruft_sweep` passes the routed argv through to subprocess.run, with
      the `--class all --apply --quiet` tail intact
  T5  `run_cruft_sweep` stays non-blocking (rc 0) when the spawn raises OSError
"""
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
    """T1-T3: launch-rung selection, asserted on both platform values so the
    guard holds regardless of which host runs the suite."""

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
    """T4-T5: the dispatch wiring around the rung. Never spawns a real sweep --
    `--apply` deletes coordinator substrate, and this box carries 50-70
    concurrent sessions."""

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
