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
chunk C1.

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
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
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
    """T1-T4: the abort-time process-group teardown wiring around
    `_run_fast_test_cmd` (see module docstring's coverage list). Exercises
    the wiring in-process via monkeypatch/fake -- never spawns a real
    xdist pool or kills real processes, per this box's shared-machine
    load posture."""

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
            # Must not raise, and restore() itself must not raise either.
            restore()

    @unittest.skipIf(os.name == "nt", "POSIX-only killpg leg")
    def test_t3_teardown_swallows_raising_killpg(self) -> None:
        mod = _load_cli_module()
        fake_proc = type("FakeProc", (), {"pid": 999999})()
        with mock.patch.object(mod.os, "killpg", side_effect=OSError("no such process group")):
            # Must not raise -- AC3: a reap that raises must never change
            # the run's exit code.
            mod._teardown_process_group(fake_proc)

    def test_t3_noop_on_windows(self) -> None:
        mod = _load_cli_module()
        fake_proc = type("FakeProc", (), {"pid": 999999})()
        with mock.patch.object(mod.os, "name", "nt"):
            # Must not raise or attempt a POSIX killpg call at all.
            with mock.patch.object(mod.os, "killpg") as fake_killpg:
                mod._teardown_process_group(fake_proc)
                fake_killpg.assert_not_called()

    def test_t4_windows_job_object_noop_off_windows(self) -> None:
        mod = _load_cli_module()
        fake_proc = type("FakeProc", (), {"pid": os.getpid()})()
        self.assertIsNone(mod._assign_windows_job_object(fake_proc))
        # Must not raise on a None handle or on a non-Windows host.
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


if __name__ == "__main__":
    unittest.main()
