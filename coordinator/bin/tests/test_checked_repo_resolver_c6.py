"""test_checked_repo_resolver_c6.py -- C6 test surface for the near-verbatim
copy scripts' repoint onto the C1 checked resolver: wsc-tail.py,
query-handoff-columns.py, workday-complete-step9-append-changelog.py,
standup.py, whats-next.py, and review-coverage-gate.py.

migrate-lessons-md-to-yaml.py's coverage was removed here when that
one-shot, caller-less entry point was deleted outright (see
docs/plans/2026-08-16-a-process-per-predicate.md § C2) rather than kept
covering a file that no longer exists.

Spec backlink: pln-one-checked-resolver-for-the-c-035d59
§ C6 / AC2-AC5, AC10.

C1's own module (`test_checked_repo_resolver.py`) already exhaustively
covers `resolve_checked_repo_root`'s verdict-construction machinery -- this
module tests representative C6 call sites, mocked at `resolve_checked_repo_root`
(the name each script binds into its own namespace via `from repo_identity
import resolve_checked_repo_root`).

query-handoff-columns.py stays READER (DR-277, AC10): MATCH/EXPLICIT proceed
silently, MISMATCH warns to stderr via the resolver's own pre-rendered
message and still proceeds with the resolved root, UNRESOLVED never refuses
(AC4).

workday-complete-step9-append-changelog.py is WRITER (AC10 reclassification
post-C8: it dispatches a real write -- changelog_ops.append_day -- reached
downstream of the checked-resolver call). A positive MISMATCH refuses BEFORE
that write, asserted here on the absence of the write's target artifact, not
merely on an exit code (mirrors C4's own absence-based assertion for its five
WRITER scripts). UNRESOLVED never refuses either (AC4).

This module is C6's own -- no other chunk in this wave writes to it (wave
parallel-safety via test-file disjointness).
"""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import sys
import tempfile
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


class TestQueryHandoffColumnsResolver(unittest.TestCase):
    """query-handoff-columns.py's `_resolve_repo_root` -- an ACTIVE PEER
    SURFACE re-read at HEAD immediately before this repoint (three commits
    landed on it the same day)."""

    module = _load_module("query-handoff-columns.py", "c6_query_handoff_columns")

    def test_mismatch_warns_and_still_proceeds_with_resolved_root(self):
        v = _verdict("MISMATCH", "/repo/mismatch")
        with mock.patch.object(
            self.module, "resolve_checked_repo_root", return_value=("/repo/mismatch", v)
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                root = self.module._resolve_repo_root()

        self.assertEqual(root, "/repo/mismatch")
        self.assertIn(v["message"], stderr.getvalue())

    def test_match_returns_resolved_root_silently(self):
        v = _verdict("MATCH", "/repo/match")
        with mock.patch.object(
            self.module, "resolve_checked_repo_root", return_value=("/repo/match", v)
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                root = self.module._resolve_repo_root()

        self.assertEqual(root, "/repo/match")
        self.assertEqual(stderr.getvalue(), "")

    def test_unresolved_with_a_root_never_refuses_and_proceeds(self):
        v = _verdict("UNRESOLVED", "/repo/unresolved", sid=None)
        with mock.patch.object(
            self.module, "resolve_checked_repo_root", return_value=("/repo/unresolved", v)
        ):
            root = self.module._resolve_repo_root()

        self.assertEqual(root, "/repo/unresolved")

    def test_unresolved_with_no_root_exits_nonzero(self):
        """No git root at all (AC4): the resolver itself never RAISES on
        UNRESOLVED -- it maps a None root to a documented sys.exit(1), the
        pre-existing failure path, not a new refusal introduced by this
        repoint."""
        v = _verdict("UNRESOLVED", None, sid=None)
        with mock.patch.object(
            self.module, "resolve_checked_repo_root", return_value=(None, v)
        ):
            with self.assertRaises(SystemExit):
                self.module._resolve_repo_root()


class TestWorkdayCompleteStep9Resolver(unittest.TestCase):
    """workday-complete-step9-append-changelog.py's `_resolve_coordinator_root`
    -- called at the top of main(), before Call 1 (compute_day_fields) and
    Call 2 (changelog_ops.append_day, the actual mutating write). WRITER
    (AC10 reclassification post-C8): a MISMATCH refuses HERE, before any
    write, asserted on the absence of the changelog write target."""

    module = _load_module(
        "workday-complete-step9-append-changelog.py", "c6_workday_complete_step9_append_changelog"
    )

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.mkdtemp(prefix="c6-step9-")
        os.chdir(self._tmp)
        self._old_coordinator_root = os.environ.pop("COORDINATOR_ROOT", None)

    def tearDown(self):
        os.chdir(self._old_cwd)
        if self._old_coordinator_root is not None:
            os.environ["COORDINATOR_ROOT"] = self._old_coordinator_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_mismatch_refuses_before_changelog_write(self):
        v = _verdict("MISMATCH", "/repo/mismatch")
        with mock.patch.object(
            self.module, "resolve_checked_repo_root", return_value=("/repo/mismatch", v)
        ):
            with self.assertRaises(SystemExit) as ctx:
                self.module._resolve_coordinator_root()

        self.assertEqual(ctx.exception.code, 1)
        week_changelog_dir = os.path.join(self._tmp, "state", "week-changelog")
        self.assertFalse(
            os.path.isdir(week_changelog_dir) and os.listdir(week_changelog_dir),
            "no week-changelog block must be written on MISMATCH",
        )

    def test_unresolved_still_proceeds(self):
        from pathlib import Path

        v = _verdict("UNRESOLVED", self._tmp, sid=None)
        with mock.patch.object(
            self.module, "resolve_checked_repo_root", return_value=(self._tmp, v)
        ):
            root = self.module._resolve_coordinator_root()

        self.assertEqual(
            Path(root).resolve(strict=False), Path(self._tmp).resolve(strict=False)
        )


if __name__ == "__main__":
    unittest.main()
