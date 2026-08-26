"""test_checked_repo_resolver_c7.py -- C7 test surface for the three
class-A goal/priority doors repointed onto the C1 checked resolver.

Spec backlink: pln-one-checked-resolver-for-the-c-035d59
§ C7 / AC2-AC5, AC10.

This module is C7's own -- no other chunk writes to it. C1's own module
(`test_checked_repo_resolver.py`) exhaustively covers
`resolve_checked_repo_root`'s verdict-construction machinery; duplicating
that here would test C1's code, not C7's. What C7 authored is three call
sites migrated onto that resolver:

  - goal-close-day.py    (WRITER: dispatches goal.close_day_apply, which
                           writes closed-goal rows -- refuses on MISMATCH)
  - priority-set.py      (WRITER: dispatches priority.set, which writes a
                           priority-ledger entry -- refuses on MISMATCH)
  - set-goal-kr-status.py (WRITER: dispatches goal.set_kr_status, a locked
                           read-modify-write on the target goal file --
                           refuses on MISMATCH)

Unlike C3's READER population, all three of C7's migrated scripts write
(via a dispatched op) into the resolved repo's state tree -- so DR-277's
named WRITER carve-out applies: MISMATCH refuses (exit 2) rather than
warn-and-proceed. UNRESOLVED never refuses either way (AC4).

emit-cadence.py and fan-out-dispatch.py are C7's judgment-call leave-alones
and carry no test here -- see the C7 dispatch report for the reasoning.
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_LIB_DIR = os.path.join(_BIN_DIR, "lib")
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

_MIGRATED_FILES = [
    "goal-close-day.py",
    "priority-set.py",
    "set-goal-kr-status.py",
]


def _load_module(filename: str, modname: str):
    path = Path(_BIN_DIR) / filename
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _verdict(verdict: str, resolved_root, sid="sess-x", session_root=None):
    return {
        "verdict": verdict,
        "session_root": session_root,
        "resolved_root": resolved_root,
        "sid": sid,
        "message": f"repo-identity (checked resolver): fake {verdict} for test",
    }


class TestPrivateResolversRemoved(unittest.TestCase):
    """Grep-shaped assertions: every migrated file imports the shared
    checked resolver and no longer defines/inlines its own."""

    def test_each_file_imports_resolve_checked_repo_root(self):
        for fname in _MIGRATED_FILES:
            text = (Path(_BIN_DIR) / fname).read_text(encoding="utf-8")
            with self.subTest(file=fname):
                self.assertIn(
                    "from repo_identity import resolve_checked_repo_root",
                    text,
                    f"{fname} does not import the C1 checked resolver",
                )

    def test_no_private_find_repo_root_def_remains(self):
        for fname in _MIGRATED_FILES:
            text = (Path(_BIN_DIR) / fname).read_text(encoding="utf-8")
            with self.subTest(file=fname):
                self.assertNotIn(
                    "def _find_repo_root(",
                    text,
                    f"{fname} still defines a private _find_repo_root",
                )

    def test_no_bare_os_path_exists_git_walk_remains(self):
        for fname in _MIGRATED_FILES:
            text = (Path(_BIN_DIR) / fname).read_text(encoding="utf-8")
            with self.subTest(file=fname):
                self.assertNotIn(
                    'os.path.join(cur, ".git")',
                    text,
                    f"{fname} still inlines a manual .git walk",
                )


class _WriterMismatchRefusesMixin:
    """Shared MISMATCH/UNRESOLVED/MATCH behaviour for a WRITER door: on
    MISMATCH it refuses (exit 2, DR-277 named carve-out) rather than
    warn-and-proceed; UNRESOLVED never refuses (AC4)."""

    filename: str
    modname: str
    cc_invoke_name = "cc_invoke"

    def setUp(self):
        self.mod = _load_module(self.filename, self.modname)

    def _argv(self) -> list[str]:
        raise NotImplementedError

    def test_mismatch_refuses(self):
        v = _verdict("MISMATCH", "/repo/mismatch")
        fake_cc_invoke = mock.Mock()
        with mock.patch.object(
            self.mod, "resolve_checked_repo_root",
            return_value=("/repo/mismatch", v),
        ), mock.patch.object(
            self.mod, self.cc_invoke_name, fake_cc_invoke,
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rc = self.mod.main(self._argv())

        self.assertEqual(rc, 2)
        self.assertIn(v["message"], stderr.getvalue())
        fake_cc_invoke.assert_not_called()

    def test_match_proceeds(self):
        v = _verdict("MATCH", "/repo/match")
        fake_cc_invoke = mock.Mock(return_value={"ok": True})
        with mock.patch.object(
            self.mod, "resolve_checked_repo_root",
            return_value=("/repo/match", v),
        ), mock.patch.object(
            self.mod, self.cc_invoke_name, fake_cc_invoke,
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rc = self.mod.main(self._argv())

        self.assertEqual(rc, 0)
        self.assertEqual(stderr.getvalue(), "")
        fake_cc_invoke.assert_called_once()

    def test_no_git_root_at_all_exits_2_without_dispatching(self):
        # Review: code-reviewer (P2, EM-ruled) — renamed from
        # test_unresolved_with_no_root_never_refuses_beyond_exit2, whose name
        # promised "never refuses" while its body asserted rc == 2 (it does
        # refuse). "No git root resolved at all" is a distinct condition from
        # AC4's "UNRESOLVED never refuses" identity-gate rule: there is
        # nowhere to write, not a mismatched identity, and refusing here is
        # the deliberate improvement over the pre-port fallback-to-cwd
        # walkers. This test locks in THAT refusal, not an AC4 violation.
        v = _verdict("UNRESOLVED", None, sid=None)
        with mock.patch.object(
            self.mod, "resolve_checked_repo_root",
            return_value=(None, v),
        ):
            rc = self.mod.main(self._argv())

        self.assertEqual(rc, 2)


class TestGoalCloseDayMismatchRefuses(_WriterMismatchRefusesMixin, unittest.TestCase):
    filename = "goal-close-day.py"
    modname = "goal_close_day_cli_c7"

    def _argv(self) -> list[str]:
        return ["--decisions", "{}"]


# RETIRED 2026-08-25: `TestPrioritySetMismatchRefuses`. C7 (2026-08-11) classified
# priority-set.py as a WRITER that must refuse on MISMATCH; C18 (2026-08-20,
# state/dispatch-briefs/2026-08-20-a-refusal-cannot-exit-zero/C18.md, DR-277 EM
# decision D5) later REMOVED that gate outright, because `priority.set` is
# scope="none" and resolves its ledger write centrally rather than from
# `cwd_repo_root` -- a MISMATCH there has nothing to advise on. The two contracts
# are in direct contradiction and C18 is the later decision, so this class is
# retired rather than reconciled. Its replacement already exists and is green:
# `tests/test_priority_set_no_cwd_gate.py`, which pins the gate's ABSENCE.
# `set-goal-kr-status.py` below is explicitly outside C18's scope (see its
# Anti-scope) and keeps its refusal.


class TestSetGoalKrStatusMismatchRefuses(_WriterMismatchRefusesMixin, unittest.TestCase):
    filename = "set-goal-kr-status.py"
    modname = "set_goal_kr_status_cli_c7"

    def _argv(self) -> list[str]:
        return [
            "--goal-file", "state/goals/some.yaml",
            "--kr-id", "kr-1",
            "--status", "on_track",
        ]


if __name__ == "__main__":
    unittest.main()
