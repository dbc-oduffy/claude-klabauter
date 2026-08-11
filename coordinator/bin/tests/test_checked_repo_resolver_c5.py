"""test_checked_repo_resolver_c5.py -- C5 test surface for the six
near-verbatim-copy scripts' repoint onto the C1 checked resolver:
append-goal-event.py, append-plan-session.py, prune-closed-bugs.py,
prune-closed-improvements.py, close-origin-stub-on-ship.py, and
coordinator-session-loe.py.

Spec backlink: docs/plans/2026-08-11-one-checked-resolver-for-the-bin-family.md
§ C5 / AC2-AC5, AC10.

C1's own module (`test_checked_repo_resolver.py`) already exhaustively
covers `resolve_checked_repo_root`'s verdict-construction machinery -- this
module tests the CALL SITES C5 authored: each script's own repo-root
resolver function, mocked at `resolve_checked_repo_root` (the name each
script binds into its own namespace via `from repo_identity import
resolve_checked_repo_root`), asserting the READER classification (DR-277,
AC10): MATCH/EXPLICIT proceed silently, MISMATCH warns to stderr via the
resolver's own pre-rendered message and still proceeds with the resolved
root, UNRESOLVED never refuses (AC4).

This module is C5's own -- no other chunk in this wave writes to it (wave
parallel-safety via test-file disjointness).
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from unittest import mock

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_LIB_DIR = os.path.join(_BIN_DIR, "lib")
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)


def _load_module(filename: str, modname: str):
    """Load one of the hyphenated bin/ scripts as an importable module by
    file path (hyphens in the filename make a bare `import` impossible)."""
    path = os.path.join(_BIN_DIR, filename)
    spec = importlib.util.spec_from_file_location(modname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)
    return module


def _verdict(verdict: str, resolved_root, sid="sess-x", session_root=None):
    return {
        "verdict": verdict,
        "session_root": session_root,
        "resolved_root": resolved_root,
        "sid": sid,
        "message": f"repo-identity (checked resolver): fake {verdict} for test",
    }


class _ResolverBranchingMixin:
    """Shared assertions for a script's zero-arg repo-root resolver
    function, run against every module under test."""

    module = None
    resolver_name = "_resolve_repo_root"
    fallback_to_cwd = False

    def _resolver(self):
        return getattr(self.module, self.resolver_name)

    def test_mismatch_warns_and_still_proceeds_with_resolved_root(self):
        v = _verdict("MISMATCH", "/repo/mismatch")
        with mock.patch.object(
            self.module, "resolve_checked_repo_root", return_value=("/repo/mismatch", v)
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                root = self._resolver()()

        self.assertEqual(root, "/repo/mismatch")
        self.assertIn(v["message"], stderr.getvalue())

    def test_match_returns_resolved_root_silently(self):
        v = _verdict("MATCH", "/repo/match")
        with mock.patch.object(
            self.module, "resolve_checked_repo_root", return_value=("/repo/match", v)
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                root = self._resolver()()

        self.assertEqual(root, "/repo/match")
        self.assertEqual(stderr.getvalue(), "")

    def test_unresolved_with_a_root_never_refuses_and_proceeds(self):
        v = _verdict("UNRESOLVED", "/repo/unresolved", sid=None)
        with mock.patch.object(
            self.module, "resolve_checked_repo_root", return_value=("/repo/unresolved", v)
        ):
            root = self._resolver()()

        self.assertEqual(root, "/repo/unresolved")

    def test_unresolved_with_no_root_never_refuses(self):
        """No git root at all (AC4): the resolver function itself must
        never raise/exit here -- some scripts fall back to os.getcwd(),
        others propagate None/exit to their own caller further up (that
        caller-level behavior is pre-existing and out of C5's scope; this
        only asserts the resolver call site itself doesn't refuse)."""
        v = _verdict("UNRESOLVED", None, sid=None)
        with mock.patch.object(
            self.module, "resolve_checked_repo_root", return_value=(None, v)
        ):
            if self.fallback_to_cwd:
                root = self._resolver()()
                self.assertEqual(root, os.getcwd())
            else:
                # sys.exit(2) is the documented failure path when no root
                # resolves at all -- pre-existing behavior, not a new
                # refusal introduced by the checked-resolver repoint.
                with self.assertRaises(SystemExit):
                    self._resolver()()


class TestAppendGoalEvent(_ResolverBranchingMixin, unittest.TestCase):
    module = _load_module("append-goal-event.py", "c5_append_goal_event")


class TestAppendPlanSession(_ResolverBranchingMixin, unittest.TestCase):
    module = _load_module("append-plan-session.py", "c5_append_plan_session")

    def test_unresolved_with_no_root_never_refuses(self):
        """This script's resolver returns None rather than exiting --
        caller (main()) maps that to exit 1, not the resolver itself."""
        v = _verdict("UNRESOLVED", None, sid=None)
        with mock.patch.object(
            self.module, "resolve_checked_repo_root", return_value=(None, v)
        ):
            root = self._resolver()()
        self.assertIsNone(root)


class TestPruneClosedBugs(_ResolverBranchingMixin, unittest.TestCase):
    module = _load_module("prune-closed-bugs.py", "c5_prune_closed_bugs")
    fallback_to_cwd = True


class TestPruneClosedImprovements(_ResolverBranchingMixin, unittest.TestCase):
    module = _load_module("prune-closed-improvements.py", "c5_prune_closed_improvements")
    fallback_to_cwd = True


class TestCloseOriginStubOnShip(_ResolverBranchingMixin, unittest.TestCase):
    module = _load_module("close-origin-stub-on-ship.py", "c5_close_origin_stub_on_ship")
    resolver_name = "_repo_root"
    fallback_to_cwd = True


class TestCoordinatorSessionLoe(_ResolverBranchingMixin, unittest.TestCase):
    module = _load_module("coordinator-session-loe.py", "c5_coordinator_session_loe")
    resolver_name = "_resolve_git_root"

    def test_unresolved_with_no_root_never_refuses(self):
        """This script's resolver returns None rather than exiting --
        caller (main()) maps that to 'not inside a git repo', exit 1."""
        v = _verdict("UNRESOLVED", None, sid=None)
        with mock.patch.object(
            self.module, "resolve_checked_repo_root", return_value=(None, v)
        ):
            root = self._resolver()()
        self.assertIsNone(root)


if __name__ == "__main__":
    unittest.main()
