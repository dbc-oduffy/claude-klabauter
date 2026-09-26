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
