"""bin/tests/test_workday_complete_step1_validate_test_red.py

Purpose: coverage for `_emit_test_red_record`/`_git_head_sha` -- the
test-red-record wiring added to workday-complete-step1-validate.py so the
fast-test gate's OWN captured (rc, combined output) writes
`state/test-red/<machine>.yaml` without a second pytest invocation.

Spec backlink: coordinator_core/ops/test_red_record.py module docstring
(cross-repo commitment 2026-07-25-claude-klabauter-to-answer-the-test-red-record-con).

Test coverage:
  T1  a green (rc=0) run writes tier "fast" with outcome=green
  T2  a test-failure (rc=3-classified) run writes runner+failing derived
      from the ALREADY-CAPTURED ft_content, no second pytest spawn
  T3  the record write is best-effort: an emitter exception (patched
      write_test_red_record) is swallowed and never raised into main()'s
      caller -- the isolation boundary the wiring brief required
  T4  main()'s own return code is byte-identical whether the emitter
      succeeds or raises (the "verdict/exit code never changes" contract),
      exercised via main() directly with the resolver/fast-test seams
      stubbed
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from unittest import mock

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_SCRIPT_DIR)
_CLI = os.path.join(_BIN_DIR, "workday-complete-step1-validate.py")


def _load_cli_module():
    spec = importlib.util.spec_from_file_location("_wc1v_test_red_under_test", _CLI)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class EmitTestRedRecordTest(unittest.TestCase):
    def test_t1_green_run_writes_outcome_green(self) -> None:
        mod = _load_cli_module()
        written = {}

        def _fake_write(**kwargs):
            written.update(kwargs)
            return kwargs

        with mock.patch.object(mod, "write_test_red_record", side_effect=_fake_write), \
             mock.patch.object(mod, "_git_head_sha", return_value="deadbeef"):
            mod._emit_test_red_record(0, "", 0)

        self.assertEqual(written["tier"], "fast")
        self.assertEqual(written["outcome"], "green")
        self.assertEqual(written["exit_code"], 0)
        self.assertEqual(written["sha"], "deadbeef")

    def test_t2_failure_run_derives_runner_and_failing_from_captured_content(self) -> None:
        mod = _load_cli_module()
        written = {}

        def _fake_write(**kwargs):
            written.update(kwargs)
            return kwargs

        ft_content = "FAILED coordinator_core/tests/test_foo.py::test_bar - AssertionError\n"
        with mock.patch.object(mod, "write_test_red_record", side_effect=_fake_write), \
             mock.patch.object(mod, "_git_head_sha", return_value="deadbeef"):
            mod._emit_test_red_record(3, ft_content, 3)

        self.assertEqual(written["outcome"], "test-failures")
        self.assertEqual(written["runner"], "pytest")
        self.assertEqual(
            written["failing"], ["coordinator_core/tests/test_foo.py::test_bar"]
        )
        self.assertEqual(written["exit_code"], 3)

    def test_t2b_build_failure_classification(self) -> None:
        mod = _load_cli_module()
        written = {}

        def _fake_write(**kwargs):
            written.update(kwargs)
            return kwargs

        with mock.patch.object(mod, "write_test_red_record", side_effect=_fake_write), \
             mock.patch.object(mod, "_git_head_sha", return_value="deadbeef"):
            mod._emit_test_red_record(2, "error: build failed", 2)

        self.assertEqual(written["outcome"], "build-failure")

    def test_t3_emitter_exception_is_swallowed(self) -> None:
        mod = _load_cli_module()
        with mock.patch.object(
            mod, "write_test_red_record", side_effect=RuntimeError("locked state/ dir")
        ), mock.patch.object(mod, "_git_head_sha", return_value="deadbeef"):
            # Must not raise.
            mod._emit_test_red_record(0, "", 0)

    def test_t4_main_exit_code_unchanged_by_emitter_failure(self) -> None:
        """The verdict/exit-code path is unaffected whether the test-red
        write succeeds or raises -- exercised end-to-end through main()
        with the resolver and fast-test spawn seams stubbed to a fixed
        test-failure shape (rc=3, classify_rc=3)."""
        mod = _load_cli_module()

        fake_resolve = type("R", (), {"returncode": 0, "stdout": f"{mod.shlex.quote(sys.executable)} -c \"pass\"\n"})()

        def _run_main_with_emitter(emitter_side_effect):
            with mock.patch.object(mod.rvc, "resolve_fast_test_cmd", return_value=fake_resolve), \
                 mock.patch.object(mod, "find_changed_test_files", return_value=[]), \
                 mock.patch.object(
                     mod,
                     "enforce_tier_u_gate",
                     return_value=type("G", (), {"proceed": True, "refusal_message": ""})(),
                 ), \
                 mock.patch.object(mod, "_run_fast_test_cmd", return_value=(3, "FAILED x::y\n")), \
                 mock.patch.object(mod, "write_test_red_record", side_effect=emitter_side_effect):
                return mod.main()

        rc_ok = _run_main_with_emitter(lambda **kw: kw)
        rc_fail = _run_main_with_emitter(RuntimeError("state/ read-only"))
        self.assertEqual(rc_ok, rc_fail)
        self.assertEqual(rc_ok, 3)


if __name__ == "__main__":
    unittest.main()
