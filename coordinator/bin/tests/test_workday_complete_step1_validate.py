"""bin/tests/test_workday_complete_step1_validate.py

Purpose: Unit tests for the abort-time process-group teardown wiring in
workday-complete-step1-validate.py's `_run_fast_test_cmd` -- the second of
the two ceremony spawn sites touched by chunk C1 of docs/plans/2026-08-13-
reap-orphaned-execnet-gateways.md. The sibling spawn site
(validate-fast-and-packageability.py) already has this coverage in
test_validate_fast_and_packageability.py's `ProcessGroupTeardownTest`; this
file closes the gap left on this module, mirroring that file's structure
and conventions rather than inventing new ones.

Spec backlink: pln-reap-orphaned-execnet-gateways-398c2c,
chunk C1. Row R13 (docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-fyi-rest.md)
adds T6: argv[0] resolution via shutil.which before Popen.

Test coverage:
  T1  _add_process_group_spawn_kwargs sets start_new_session on every
      platform, and additionally ORs CREATE_NEW_PROCESS_GROUP into
      creationflags when modelling Windows
  T2  _install_group_teardown installs SIGTERM/SIGINT handlers and its
      restore() reinstates the prior disposition
  T3  _teardown_process_group swallows a raising os.killpg (AC3: a reap
      that raises must never change the run's exit code)
  T4  _assign_windows_job_object / _close_windows_job_object are no-ops
      on a non-Windows host (this dev machine) and never raise
  T5  _run_fast_test_cmd preserves rc=0/rc=N/rc=127 end-to-end through the
      Popen-based spawn -- mirrors T16 in the sibling file's
      ProcessGroupTeardownTest, the end-to-end proof of AC3's exit-code-
      identity contract that this file's earlier tests exercise only at
      the `_teardown_process_group` unit level.
  T6  _run_fast_test_cmd resolves argv[0] via shutil.which before Popen --
      a fake `pnpm.CMD` shim placed on PATH resolves and runs, and an
      unresolved argv[0] fails fast with rc=127 and a one-line message,
      without ever reaching Popen.
"""
from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_SCRIPT_DIR)
_CLI = os.path.join(_BIN_DIR, "workday-complete-step1-validate.py")


def _load_cli_module():
    spec = importlib.util.spec_from_file_location("_wc1v_teardown_under_test", _CLI)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class ProcessGroupTeardownTest(unittest.TestCase):

    def test_t1_start_new_session_always_set(self) -> None:
        mod = _load_cli_module()
        kwargs: dict = {}
        mod._add_process_group_spawn_kwargs(kwargs)
        self.assertTrue(kwargs["start_new_session"])

    def test_t1_windows_ors_create_new_process_group(self) -> None:
        mod = _load_cli_module()
        with mock.patch.object(mod.os, "name", "nt"):
            kwargs = {"creationflags": 0x08000000}  # pretend CREATE_NO_WINDOW already set
            mod._add_process_group_spawn_kwargs(kwargs)
            create_new_pgroup = getattr(mod.subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            self.assertEqual(kwargs["creationflags"], 0x08000000 | create_new_pgroup)

    @unittest.skipIf(os.name == "nt", "POSIX-only signal-handler leg")
    def test_t2_install_and_restore_signal_handlers(self) -> None:
        import signal as _signal

        mod = _load_cli_module()
        orig_term = _signal.getsignal(_signal.SIGTERM)
        orig_int = _signal.getsignal(_signal.SIGINT)
        try:
            fake_proc = type("FakeProc", (), {"pid": os.getpid() + 1})()
            restore = mod._install_group_teardown(fake_proc)
            self.assertIsNot(_signal.getsignal(_signal.SIGTERM), orig_term)
            self.assertIsNot(_signal.getsignal(_signal.SIGINT), orig_int)
            restore()
            self.assertEqual(_signal.getsignal(_signal.SIGTERM), orig_term)
            self.assertEqual(_signal.getsignal(_signal.SIGINT), orig_int)
        finally:
            _signal.signal(_signal.SIGTERM, orig_term)
            _signal.signal(_signal.SIGINT, orig_int)

    def test_t2_noop_on_windows(self) -> None:
        mod = _load_cli_module()
        with mock.patch.object(mod.os, "name", "nt"):
            fake_proc = type("FakeProc", (), {"pid": os.getpid() + 1})()
            restore = mod._install_group_teardown(fake_proc)
            restore()

    @unittest.skipIf(os.name == "nt", "POSIX-only killpg leg")
    def test_t3_teardown_swallows_raising_killpg(self) -> None:
        mod = _load_cli_module()
        fake_proc = type("FakeProc", (), {"pid": 999999})()
        with mock.patch.object(mod.os, "killpg", side_effect=OSError("no such process group")):
            mod._teardown_process_group(fake_proc)

    def test_t3_noop_on_windows(self) -> None:
        mod = _load_cli_module()
        fake_proc = type("FakeProc", (), {"pid": 999999})()
        with mock.patch.object(mod.os, "name", "nt"):
            with mock.patch.object(mod.os, "killpg", create=True) as fake_killpg:
                mod._teardown_process_group(fake_proc)
                fake_killpg.assert_not_called()

    def test_t4_windows_job_object_noop_off_windows(self) -> None:
        mod = _load_cli_module()
        fake_proc = type("FakeProc", (), {"pid": os.getpid()})()
        self.assertIsNone(mod._assign_windows_job_object(fake_proc))
        mod._close_windows_job_object(None)
        mod._close_windows_job_object("not-a-real-handle")

    def test_t5_run_fast_test_cmd_preserves_exit_codes(self) -> None:
        mod = _load_cli_module()
        env = dict(os.environ)

        py = mod.shlex.quote(sys.executable)
        rc, _content = mod._run_fast_test_cmd(f"{py} -c \"import sys; sys.exit(0)\"", env)
        self.assertEqual(rc, 0)

        rc, _content = mod._run_fast_test_cmd(f"{py} -c \"import sys; sys.exit(3)\"", env)
        self.assertEqual(rc, 3)

        rc, content = mod._run_fast_test_cmd(
            "this-binary-does-not-exist-anywhere-12345", env
        )
        self.assertEqual(rc, 127)
        self.assertIn("command not found", content)


class ArgvWhichResolutionTest(unittest.TestCase):

    def test_t6_fake_cmd_shim_on_path_resolves_and_runs(self) -> None:
        mod = _load_cli_module()
        env = dict(os.environ)
        with tempfile.TemporaryDirectory() as shim_dir:
            shim_name = "pnpm-fake-shim.CMD" if os.name == "nt" else "pnpm-fake-shim"
            shim_path = os.path.join(shim_dir, shim_name)
            with open(shim_path, "w") as f:
                f.write(f"#!{sys.executable}\nimport sys; sys.exit(0)\n")
            os.chmod(shim_path, os.stat(shim_path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            env["PATH"] = shim_dir + os.pathsep + env.get("PATH", "")
            with mock.patch.object(mod.subprocess, "Popen") as fake_popen:
                fake_proc = mock.Mock()
                fake_proc.communicate.return_value = ("", "")
                fake_proc.returncode = 0
                fake_proc.pid = os.getpid()
                fake_popen.return_value = fake_proc
                rc, _content = mod._run_fast_test_cmd("pnpm-fake-shim run test", env)
            self.assertEqual(rc, 0)
            fake_popen.assert_called_once()
            called_argv = fake_popen.call_args[0][0]
            self.assertEqual(called_argv[0], shim_path)

    def test_t6_unresolved_argv0_fails_before_popen(self) -> None:
        mod = _load_cli_module()
        env = dict(os.environ)
        with mock.patch.object(mod.subprocess, "Popen") as fake_popen:
            rc, content = mod._run_fast_test_cmd(
                "this-binary-does-not-exist-anywhere-12345 run test", env
            )
            fake_popen.assert_not_called()
        self.assertEqual(rc, 127)
        self.assertIn("command not found", content)


if __name__ == "__main__":
    unittest.main()
