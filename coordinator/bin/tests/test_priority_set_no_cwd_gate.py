"""test_priority_set_no_cwd_gate.py -- C18 test surface: priority-set.py's
cwd identity gate is removed.

Spec backlink: state/dispatch-briefs/2026-08-20-a-refusal-cannot-exit-zero/C18.md
(DR-277, EM decision D5).

`priority.set` is scope="none" (coordinator_core/ops/priority_set.py): the
ledger write is resolved centrally (`coordinator_state_root(central=True)`),
never derived from `cwd_repo_root`. The MISMATCH branch that used to refuse
exit 2 here had nothing to advise on, so it was removed outright (not
demoted to a warning). This module asserts: MATCH, MISMATCH, and UNRESOLVED
verdicts all now proceed to `cc_invoke` (main() returns 0 on a successful
op call, and NEVER exit 2 on a MISMATCH verdict) -- the only remaining exit
2 is the pre-existing "no git root at all" (`cwd_repo_root is None`) path,
which is unrelated to identity and stays intact.

Negative-spec: this module does NOT touch set-goal-kr-status.py's own gate
(out of scope, see C18's Anti-scope) and does not assert anything about
`resolve_checked_repo_root`'s own verdict-construction machinery (covered by
`test_checked_repo_resolver.py`).
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_LIB_DIR = os.path.join(_BIN_DIR, "lib")
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)


def _load_module():
    path = os.path.join(_BIN_DIR, "priority-set.py")
    spec = importlib.util.spec_from_file_location("c18_priority_set", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["c18_priority_set"] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _load_module()

_ARGV = ["--target-id", "t1", "--target-kind", "handoff", "--priority", "high"]

_RESULT = {
    "target_id": "t1",
    "target_kind": "handoff",
    "priority": "high",
    "set_by": "",
    "source": "cli",
}


def _verdict(verdict: str, resolved_root, sid="sess-x"):
    return {
        "verdict": verdict,
        "session_root": None,
        "resolved_root": resolved_root,
        "sid": sid,
        "message": f"repo-identity (checked resolver): fake {verdict} for test",
    }


class TestPriorityKrSetNoCwdGate(unittest.TestCase):
    def test_mismatch_no_longer_refuses(self):
        """The removed assertion: a MISMATCH verdict used to exit 2 here.
        It must now proceed to cc_invoke exactly like MATCH."""
        v = _verdict("MISMATCH", "/repo/mismatch")
        with mock.patch.object(
            _MODULE, "resolve_checked_repo_root", return_value=("/repo/mismatch", v)
        ), mock.patch.object(_MODULE, "cc_invoke", return_value=_RESULT) as m_invoke:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                rc = _MODULE.main(list(_ARGV))

        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(stdout.getvalue()), _RESULT)
        m_invoke.assert_called_once()
        self.assertEqual(m_invoke.call_args[0][2], "/repo/mismatch")

    def test_match_proceeds(self):
        v = _verdict("MATCH", "/repo/match")
        with mock.patch.object(
            _MODULE, "resolve_checked_repo_root", return_value=("/repo/match", v)
        ), mock.patch.object(_MODULE, "cc_invoke", return_value=_RESULT):
            rc = _MODULE.main(list(_ARGV))

        self.assertEqual(rc, 0)

    def test_unresolved_with_a_root_never_refuses(self):
        v = _verdict("UNRESOLVED", "/repo/unresolved", sid=None)
        with mock.patch.object(
            _MODULE, "resolve_checked_repo_root", return_value=("/repo/unresolved", v)
        ), mock.patch.object(_MODULE, "cc_invoke", return_value=_RESULT):
            rc = _MODULE.main(list(_ARGV))

        self.assertEqual(rc, 0)

    def test_no_git_root_at_all_still_refuses(self):
        """The one remaining exit-2 path: distinct from the removed MISMATCH
        gate -- this is "nowhere to spawn from", not an identity check."""
        v = _verdict("UNRESOLVED", None, sid=None)
        with mock.patch.object(
            _MODULE, "resolve_checked_repo_root", return_value=(None, v)
        ), mock.patch.object(_MODULE, "cc_invoke") as m_invoke:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rc = _MODULE.main(list(_ARGV))

        self.assertEqual(rc, 2)
        m_invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
