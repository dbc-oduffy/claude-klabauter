"""test_timeout_is_not_absence.py — a timeout on prune-closed-bugs.py's ACT
call must report indeterminate, never a completed/absent archive count.

Defect this closes (C13, plan 2026-08-20-a-refusal-cannot-exit-zero): the ACT
call's `except RuntimeError` handler collapsed every transport failure --
including `cc_invoke.is_timeout_error`'s own TimeoutExpired-derived
RuntimeError -- into "N candidate(s) selected but not archived (transport
error)". CLAUDE.md § Load norm: a timeout is a SLOW op, not a stopped one --
the op may be mid-`git mv`+commit and about to succeed, so reporting "not
archived" here is a false negative that re-dispatches the same ids on the
next sweep. `is_timeout_error` (C7/C8) is the discriminator that lets this
script distinguish "engine was simply busy" from every other transport
failure, without re-deriving or substring-matching cc_invoke's error text.

Spec backlink: state/dispatch-briefs/2026-08-20-a-refusal-cannot-exit-zero/C13.md
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


class _RouteMutationStub:
    """Stand-in for cc_invoke.route_mutation: the dry-run preview call,
    returning one candidate."""

    def __call__(self, op, params, repo_root, fallback):
        return {"candidates": [{"id": "state/x.md"}], "exit_code": 0}


class PruneClosedBugsTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module("prune-closed-bugs.py", "prune_closed_bugs_c13_test")
        self.mod._resolve_repo_root = lambda: "/fake-repo"
        self.mod.route_mutation = _RouteMutationStub()

    def test_act_timeout_reports_indeterminate_not_absence(self):
        timeout_exc = RuntimeError("cc_invoke: engine timeout after 30s")
        self.assertTrue(self.mod.is_timeout_error(timeout_exc))

        def _route_raises(op, params, repo_root, fallback):
            raise timeout_exc

        # `self.mod.cc_invoke` is the process-wide `cc_invoke` module every
        # coordinator/bin script imports by bare name — assigning `route` on
        # it without a restore poisons every later test in the same pytest
        # worker, which is why this goes through patch.object + addCleanup.
        patcher = unittest.mock.patch.object(self.mod.cc_invoke, "route", _route_raises)
        patcher.start()
        self.addCleanup(patcher.stop)

        buf = []
        import builtins

        real_print = builtins.print

        def _capture_print(*args, **kwargs):
            buf.append(" ".join(str(a) for a in args))

        builtins.print = _capture_print
        try:
            rc = self.mod.main([])
        finally:
            builtins.print = real_print

        self.assertEqual(rc, 0)
        stdout_lines = "\n".join(buf)
        self.assertNotIn("not archived", stdout_lines)
        self.assertNotIn("0 entr(ies) archived", stdout_lines)
        self.assertIn("indeterminate", stdout_lines)

    def test_act_non_timeout_transport_failure_still_reports_not_archived(self):
        other_exc = RuntimeError("cc_invoke: engine wont start")

        def _route_raises(op, params, repo_root, fallback):
            raise other_exc

        # `self.mod.cc_invoke` is the process-wide `cc_invoke` module every
        # coordinator/bin script imports by bare name — assigning `route` on
        # it without a restore poisons every later test in the same pytest
        # worker, which is why this goes through patch.object + addCleanup.
        patcher = unittest.mock.patch.object(self.mod.cc_invoke, "route", _route_raises)
        patcher.start()
        self.addCleanup(patcher.stop)

        buf = []
        import builtins

        real_print = builtins.print

        def _capture_print(*args, **kwargs):
            buf.append(" ".join(str(a) for a in args))

        builtins.print = _capture_print
        try:
            rc = self.mod.main([])
        finally:
            builtins.print = real_print

        self.assertEqual(rc, 0)
        stdout_lines = "\n".join(buf)
        self.assertIn("not archived", stdout_lines)


if __name__ == "__main__":
    unittest.main()
