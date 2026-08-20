"""test_mutating_caller_refusal_ladder.py — the DR-215 exit_code trap, closed
at the caller (C12, docs/plans/2026-08-20-a-refusal-cannot-exit-zero.md).

Four mutating `coordinator/bin/*.py` CLIs dispatch a MUTATING op through
`cc_invoke.route()`/`cc_invoke.cc_invoke()` — neither of which raises on an
in-envelope refusal (`{"exit_code": N != 0}` / `{"error": ...}` nested inside
an otherwise-successful transport result). Before this chunk each caller
either printed the refusal as an ordinary success payload and exited 0
(`goal-close-day.py`, `priority-set.py`, `set-goal-kr-status.py`), or
discarded the result entirely (`reap-sessions.py`).

Tests:
    reap-sessions.py     — refusal logged to stderr, exit 0 (module's own
                            "never block session start" contract; not a
                            silent discard any more — see the negative test).
    goal-close-day.py    — refusal -> exit 2, refusal message on stderr.
    priority-set.py      — refusal -> exit 2, refusal message on stderr.
    set-goal-kr-status.py — refusal -> exit 2, refusal message on stderr.
    All four              — an ordinary success result still exits 0.

Spec backlink: state/dispatch-briefs/2026-08-20-a-refusal-cannot-exit-zero/C12.md
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
import unittest
import unittest.mock

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_LIB_DIR = os.path.join(_BIN_DIR, "lib")

if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import cc_invoke as _cc_invoke_mod  # noqa: E402


def _load_cli_module(name: str, filename: str):
    """Import a hyphen-named `coordinator/bin/*.py` CLI by file path — not
    importable by dotted name, and side-effect-free at import (`main()` is
    `__main__`-guarded)."""
    path = os.path.join(_BIN_DIR, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REFUSAL_ENVELOPE = {"exit_code": 1, "error": "op refused"}
SUCCESS_ENVELOPE = {"ok": True}


class TestReapSessionsInspectsRefusal(unittest.TestCase):
    """reap-sessions.py: MUST NOT block session start (exit 0 always), but
    MUST NOT silently discard an in-envelope refusal any more either."""

    def setUp(self) -> None:
        self.mod = _load_cli_module("_cli_reap_sessions", "reap-sessions.py")

    def test_refusal_logged_not_discarded_still_exits_0(self) -> None:
        stderr = io.StringIO()
        with unittest.mock.patch.object(
            self.mod, "_resolve_repo_root", return_value="/fake/repo"
        ), unittest.mock.patch.object(
            self.mod.cc_invoke, "route", return_value=REFUSAL_ENVELOPE
        ), unittest.mock.patch("sys.stderr", stderr):
            rc = self.mod.main([])

        self.assertEqual(rc, 0)
        self.assertIn("op refused", stderr.getvalue())

    def test_success_result_exits_0_no_refusal_text(self) -> None:
        stderr = io.StringIO()
        with unittest.mock.patch.object(
            self.mod, "_resolve_repo_root", return_value="/fake/repo"
        ), unittest.mock.patch.object(
            self.mod.cc_invoke, "route", return_value=SUCCESS_ENVELOPE
        ), unittest.mock.patch("sys.stderr", stderr):
            rc = self.mod.main([])

        self.assertEqual(rc, 0)
        self.assertNotIn("refused", stderr.getvalue())

    def test_transport_failure_still_exits_0(self) -> None:
        """Unchanged pre-existing contract: a raised RuntimeError from route()
        (transport failure) is still logged and swallowed to exit 0."""
        stderr = io.StringIO()
        with unittest.mock.patch.object(
            self.mod, "_resolve_repo_root", return_value="/fake/repo"
        ), unittest.mock.patch.object(
            self.mod.cc_invoke, "route", side_effect=RuntimeError("transport down")
        ), unittest.mock.patch("sys.stderr", stderr):
            rc = self.mod.main([])

        self.assertEqual(rc, 0)
        self.assertIn("transport down", stderr.getvalue())


class _ExitTwoOnRefusalMixin:
    """Shared assertions for the three thin CLI doors that call
    `cc_invoke.cc_invoke()` directly (not `route()`), print the bare result,
    and exit 0 unconditionally on transport success — before this chunk."""

    module_name: str
    filename: str
    op_name: str

    def setUp(self) -> None:
        self.mod = _load_cli_module(self.module_name, self.filename)

    def _patch_repo_root(self):
        return unittest.mock.patch.object(
            self.mod,
            "resolve_checked_repo_root",
            return_value=("/fake/repo", {"verdict": "OK", "message": ""}),
        )

    def test_refusal_exits_2_with_message_on_stderr(self) -> None:
        stderr = io.StringIO()
        with self._patch_repo_root(), unittest.mock.patch.object(
            self.mod, "cc_invoke", return_value=REFUSAL_ENVELOPE
        ), unittest.mock.patch("sys.stderr", stderr):
            rc = self.mod.main(self._argv())

        self.assertEqual(rc, 2)
        self.assertIn("op refused", stderr.getvalue())

    def test_success_result_exits_0(self) -> None:
        with self._patch_repo_root(), unittest.mock.patch.object(
            self.mod, "cc_invoke", return_value=SUCCESS_ENVELOPE
        ):
            rc = self.mod.main(self._argv())

        self.assertEqual(rc, 0)

    def _argv(self) -> list[str]:
        raise NotImplementedError


class TestGoalCloseDayInspectsRefusal(_ExitTwoOnRefusalMixin, unittest.TestCase):
    module_name = "_cli_goal_close_day"
    filename = "goal-close-day.py"
    op_name = "goal.close_day_apply"

    def _argv(self) -> list[str]:
        return ["--decisions", "{}"]


class TestPrioritySetInspectsRefusal(_ExitTwoOnRefusalMixin, unittest.TestCase):
    module_name = "_cli_priority_set"
    filename = "priority-set.py"
    op_name = "priority.set"

    def _argv(self) -> list[str]:
        return ["--target-id", "id-1", "--target-kind", "handoff", "--priority", "high"]


class TestSetGoalKrStatusInspectsRefusal(_ExitTwoOnRefusalMixin, unittest.TestCase):
    module_name = "_cli_set_goal_kr_status"
    filename = "set-goal-kr-status.py"
    op_name = "goal.set_kr_status"

    def _argv(self) -> list[str]:
        return ["--goal-file", "/fake/goal.yaml", "--kr-id", "kr-1", "--status", "done"]


class TestMutationRefusalMessageHelper(unittest.TestCase):
    """cc_invoke.mutation_refusal_message — the extracted shared inspection
    every caller above now uses, mirrored against route_mutation's own two
    documented refusal shapes."""

    def test_nonzero_exit_code_shape_refuses(self) -> None:
        message = _cc_invoke_mod.mutation_refusal_message("op.name", {"exit_code": 2, "failed": [{}]})
        self.assertIsNotNone(message)
        self.assertIn("op.name", message)

    def test_error_field_absent_exit_code_shape_refuses(self) -> None:
        message = _cc_invoke_mod.mutation_refusal_message("op.name", {"error": "boom"})
        self.assertIsNotNone(message)
        self.assertIn("boom", message)

    def test_ordinary_success_returns_none(self) -> None:
        message = _cc_invoke_mod.mutation_refusal_message("op.name", {"ok": True})
        self.assertIsNone(message)

    def test_non_dict_result_returns_none(self) -> None:
        message = _cc_invoke_mod.mutation_refusal_message("op.name", "not-a-dict")
        self.assertIsNone(message)


if __name__ == "__main__":
    unittest.main()
