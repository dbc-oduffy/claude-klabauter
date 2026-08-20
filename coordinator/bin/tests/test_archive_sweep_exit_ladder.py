"""test_archive_sweep_exit_ladder.py — the three archive sweeps must refuse
(non-zero exit, no liveness stamp) on an unrecognized `exit_code`, not just
on `exit_code == 2`.

Defect this closes (C11, plan 2026-08-20-a-refusal-cannot-exit-zero): each of
sweep-terminal-plans.py, sweep-actioned-memos.py, and prune-closed-bugs.py
checked `act_exit == 2` (DETERMINATE-PARTIAL) and nothing else on the ACT
call's exit_code, so `exit_code == 1` -- the op's setup-error shape
(state/lessons/2026-07-14-destructive-engine-ops-must-fail-closed-a5fa9ceef812.yaml's
fail-closed-refusal prescription) -- fell through the `if`, printed the acted
count (0), stamped `_stamp_archive_sweeps_liveness` (where applicable), and
exited 0. A refusal read as a healthy "nothing was terminal" sweep, with the
liveness marker advancing so nothing downstream can tell the sweep did not
run.

Each module is loaded by file path (hyphenated, not import-able as a normal
module) and its `cc_invoke.route` / `route_mutation` / `_resolve_repo_root` /
`_stamp_archive_sweeps_liveness` seams are monkeypatched so this suite never
needs a real claude-klabauter checkout or engine call -- it asserts only the
exit_code -> refusal-shape translation.

Spec backlink: state/dispatch-briefs/2026-08-20-a-refusal-cannot-exit-zero/C11.md
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
import unittest.mock
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module(filename: str, alias: str):
    loader = importlib.machinery.SourceFileLoader(alias, str(_BIN_DIR / filename))
    spec = importlib.util.spec_from_loader(alias, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def _install_route(testcase, mod, stub):
    """Point the subject's `cc_invoke.route` at `stub` for one test only.

    `mod.cc_invoke` is the process-wide `cc_invoke` module every
    coordinator/bin script imports by bare name — a bare assignment here
    outlives the test and poisons every later one in the same pytest worker
    (the subject module object is fresh per test; the library it imported is
    not). Restore is registered as a cleanup, never left to the test body.
    """
    patcher = unittest.mock.patch.object(mod.cc_invoke, "route", stub)
    patcher.start()
    testcase.addCleanup(patcher.stop)


class _RouteStub:
    """Stand-in for cc_invoke.route: first call is the dry-run preview
    (one candidate, exit_code 0), second call is the ACT call whose
    exit_code is under test."""

    def __init__(self, act_exit_code):
        self._act_exit_code = act_exit_code
        self.calls = 0

    def __call__(self, op, params, repo_root, fallback):
        self.calls += 1
        if params.get("dry_run"):
            return {"candidates": [{"id": "state/x.md"}], "exit_code": 0}
        return {"acted": [], "exit_code": self._act_exit_code}


class SweepTerminalPlansExitLadderTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module("sweep-terminal-plans.py", "sweep_terminal_plans_c11_test")
        self.mod._resolve_repo_root = lambda positional: "/fake-repo"
        self.stamped = []
        self.mod._stamp_archive_sweeps_liveness = lambda repo_root: self.stamped.append(repo_root)

    def test_act_exit_1_refuses_non_zero_and_no_stamp(self):
        stub = _RouteStub(1)
        _install_route(self, self.mod, stub)
        rc = self.mod.main([])
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.stamped, [])

    def test_act_exit_2_still_stamps_and_exits_zero(self):
        stub = _RouteStub(2)
        _install_route(self, self.mod, stub)
        rc = self.mod.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(self.stamped, ["/fake-repo"])


class SweepActionedMemosExitLadderTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module("sweep-actioned-memos.py", "sweep_actioned_memos_c11_test")
        self.mod._resolve_repo_root = lambda positional: "/fake-repo"
        self.stamped = []
        self.mod._stamp_archive_sweeps_liveness = lambda repo_root: self.stamped.append(repo_root)

    def test_act_exit_1_refuses_non_zero_and_no_stamp(self):
        stub = _RouteStub(1)
        _install_route(self, self.mod, stub)
        rc = self.mod.main([])
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.stamped, [])

    def test_act_exit_2_still_stamps_and_exits_zero(self):
        stub = _RouteStub(2)
        _install_route(self, self.mod, stub)
        rc = self.mod.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(self.stamped, ["/fake-repo"])


class PruneClosedBugsExitLadderTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module("prune-closed-bugs.py", "prune_closed_bugs_c11_test")
        self.mod._resolve_repo_root = lambda: "/fake-repo"

        def _route_mutation_stub(op, params, repo_root, fallback):
            return {"candidates": [{"id": "state/x.md"}], "exit_code": 0}

        self.mod.route_mutation = _route_mutation_stub

    def test_act_exit_1_refuses_non_zero(self):
        stub = _RouteStub(1)
        _install_route(self, self.mod, stub)
        rc = self.mod.main([])
        self.assertNotEqual(rc, 0)

    def test_act_exit_2_still_exits_zero(self):
        stub = _RouteStub(2)
        _install_route(self, self.mod, stub)
        rc = self.mod.main([])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
