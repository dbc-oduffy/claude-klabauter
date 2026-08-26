"""test_checked_repo_resolver_c3.py -- C3 test surface for the sweep/reap
scripts and the ceremony hook repointed onto the C1 checked resolver.

Spec backlink: pln-one-checked-resolver-for-the-c-035d59
§ C3 / AC2-AC5, AC10.

This module is C3's own -- no other chunk in this wave writes to it. C1's
own module (`test_checked_repo_resolver.py`) exhaustively covers
`resolve_checked_repo_root`'s verdict-construction machinery; duplicating
that here would test C1's code, not C3's. What C3 authored is seven call
sites migrated onto that resolver. This module:

  1. Grep-shaped assertions that the private per-script resolvers /
     inline `git rev-parse` + `os.getcwd()` fallbacks are gone from all
     seven migrated files, and that each now imports
     `resolve_checked_repo_root` from `repo_identity`.
  2. A representative wrong-repo (MISMATCH) case for `baton-drift-sweep.py`
     asserting the READER classification (AC10, DR-277): warn to stderr
     using the resolver's own pre-rendered message, then PROCEED (never
     refuse) with the resolved root.
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

_FAKE_SWEEP_RESULT = {
    "total_live": 0,
    "terminal_not_archived": 0,
    "non_terminal": 0,
    "held": 0,
    "stranded": 0,
    "stranded_paths": [],
    "never_started": 0,
    "never_started_paths": [],
    "reconciled_no_successor": 0,
    "reconciled_no_successor_paths": [],
    "tips": [],
}

_MIGRATED_FILES = [
    "baton-drift-sweep.py",
    "day-coverage-sweep.py",
    # sweep-shipped-handoffs.py was deleted with the killed archiver (648f2e4eb,
    # "delete the killed archiver, sweep its callers") -- this roster is one of the
    # callers that sweep missed, which is the A-KILLED-OP-IS-WHAT-IT-MUTATED-NOT-WHAT-
    # IT-DID shape: the module went, its readers stayed, and nothing was red until
    # someone reached for the surface.
    "reap-integrated-review-findings.py",
    "reap-orphaned-in-flight-handoffs.py",
    "reap-stale-subagent-sidecars.py",
    "coordinator-ceremony-hook.py",
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

    def test_no_private_resolve_repo_root_def_remains(self):
        for fname in _MIGRATED_FILES:
            text = (Path(_BIN_DIR) / fname).read_text(encoding="utf-8")
            with self.subTest(file=fname):
                self.assertNotIn(
                    "def _resolve_repo_root(",
                    text,
                    f"{fname} still defines a private _resolve_repo_root",
                )

    def test_no_bare_git_rev_parse_show_toplevel_spawn_remains(self):
        """None of the seven files should still spawn a fresh
        `git rev-parse --show-toplevel` themselves -- that primitive now
        lives solely inside `repo_identity`/`show_toplevel`."""
        for fname in _MIGRATED_FILES:
            text = (Path(_BIN_DIR) / fname).read_text(encoding="utf-8")
            with self.subTest(file=fname):
                self.assertNotIn('"rev-parse", "--show-toplevel"', text)
                self.assertNotIn("'rev-parse', '--show-toplevel'", text)


class TestBatonDriftSweepMismatchWarnsAndProceeds(unittest.TestCase):
    """Representative wrong-repo case (AC10, DR-277): baton-drift-sweep.py
    is a READER -- on MISMATCH it warns to stderr and proceeds (exit 0),
    it never refuses. UNRESOLVED never refuses either (AC4)."""

    def setUp(self):
        self.mod = _load_module("baton-drift-sweep.py", "baton_drift_sweep_cli_c3")

    def test_mismatch_warns_and_proceeds(self):
        v = _verdict("MISMATCH", "/repo/mismatch")
        fake_sweep = mock.Mock(return_value=_FAKE_SWEEP_RESULT.copy())
        with mock.patch.object(
            self.mod, "resolve_checked_repo_root",
            return_value=("/repo/mismatch", v),
        ), mock.patch.object(
            self.mod, "_import_baton_drift_sweep", return_value=fake_sweep,
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rc = self.mod.main(["baton-drift-sweep.py"])

        self.assertEqual(rc, 0)
        self.assertIn(v["message"], stderr.getvalue())
        fake_sweep.assert_called_once()

    def test_match_proceeds_silently(self):
        v = _verdict("MATCH", "/repo/match")
        fake_sweep = mock.Mock(return_value=_FAKE_SWEEP_RESULT.copy())
        with mock.patch.object(
            self.mod, "resolve_checked_repo_root",
            return_value=("/repo/match", v),
        ), mock.patch.object(
            self.mod, "_import_baton_drift_sweep", return_value=fake_sweep,
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rc = self.mod.main(["baton-drift-sweep.py"])

        self.assertEqual(rc, 0)
        self.assertEqual(stderr.getvalue(), "")

    def test_unresolved_with_no_root_never_refuses(self):
        """AC4/DR-277: UNRESOLVED with no root at all still returns
        cleanly (repo-root-unresolvable exit 2), it is never hardened
        into a crash/refusal path beyond the pre-existing exit-2 contract."""
        v = _verdict("UNRESOLVED", None, sid=None)
        with mock.patch.object(
            self.mod, "resolve_checked_repo_root",
            return_value=(None, v),
        ):
            rc = self.mod.main(["baton-drift-sweep.py"])

        self.assertEqual(rc, 2)


class TestCeremonyHookMismatchWarnsAndProceeds(unittest.TestCase):
    """coordinator-ceremony-hook.py is the hottest script in this slice
    (dispatch brief). Same READER shape as baton-drift-sweep.py: MISMATCH
    warns to stderr using the resolver's own message and proceeds
    (exit 0, never refuses); UNRESOLVED with no root falls back to
    os.getcwd() and also never refuses (AC4). The resolver import here is
    deferred (inside main(), after two early no-op returns), so this
    mocks the shared repo_identity module's attribute rather than a
    module-level name on the ceremony-hook module itself."""

    def setUp(self):
        self.mod = _load_module("coordinator-ceremony-hook.py", "coordinator_ceremony_hook_cli_c3")
        import repo_identity
        self.repo_identity = repo_identity

    def test_mismatch_warns_and_proceeds(self):
        v = _verdict("MISMATCH", "/repo/mismatch")
        with mock.patch.object(
            self.repo_identity, "resolve_checked_repo_root",
            return_value=("/repo/mismatch", v),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rc = self.mod.main(["workday-start"])

        self.assertEqual(rc, 0)
        self.assertIn(v["message"], stderr.getvalue())

    def test_match_proceeds_silently(self):
        v = _verdict("MATCH", "/repo/match")
        with mock.patch.object(
            self.repo_identity, "resolve_checked_repo_root",
            return_value=("/repo/match", v),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rc = self.mod.main(["workday-start"])

        self.assertEqual(rc, 0)
        self.assertNotIn(v["message"], stderr.getvalue())

    def test_unresolved_with_no_root_never_refuses(self):
        """AC4/DR-277: UNRESOLVED with no root at all falls back to
        os.getcwd() and still returns 0 -- never hardened into a
        crash/refusal beyond this hook's exit-0-always contract."""
        v = _verdict("UNRESOLVED", None, sid=None)
        with mock.patch.object(
            self.repo_identity, "resolve_checked_repo_root",
            return_value=(None, v),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rc = self.mod.main(["workday-start"])

        self.assertEqual(rc, 0)


class TestReapIntegratedReviewFindingsMismatchWarnsAndProceeds(unittest.TestCase):
    """reap-integrated-review-findings.py has two distinct call sites
    (_reap_integrated_legacy, _reap_native) behind a seam-presence branch
    in main(); neither was exercised by the grep-shaped assertions above.
    Both are READERs (AC10, DR-277): MISMATCH warns and proceeds,
    UNRESOLVED never refuses. Calling each function directly (bypassing
    main()'s seam probe) with an empty tmp dir as repo_root keeps both
    on the cheap "findings_dir does not exist; nothing to do" / native
    transport-mocked early-exit path, same fixture shape as the ceremony
    hook above -- no real git scaffolding needed."""

    def setUp(self):
        self.mod = _load_module(
            "reap-integrated-review-findings.py", "reap_integrated_review_findings_cli_c3"
        )

    def test_legacy_mismatch_warns_and_proceeds(self):
        v = _verdict("MISMATCH", "/repo/mismatch")
        with mock.patch.object(
            self.mod, "resolve_checked_repo_root",
            return_value=("/repo/mismatch-does-not-exist", v),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rc = self.mod._reap_integrated_legacy(dry_run=False, commit_prefix="")

        self.assertEqual(rc, 0)
        self.assertIn(v["message"], stderr.getvalue())

    def test_legacy_unresolved_with_no_root_never_refuses(self):
        v = _verdict("UNRESOLVED", None, sid=None)
        with mock.patch.object(
            self.mod, "resolve_checked_repo_root",
            return_value=(None, v),
        ):
            rc = self.mod._reap_integrated_legacy(dry_run=False, commit_prefix="")

        self.assertEqual(rc, 0)

    def test_native_mismatch_warns_and_proceeds(self):
        v = _verdict("MISMATCH", "/repo/mismatch")
        fake_result = {"exit_code": 0, "reaped": [], "reaped_total": 0}
        with mock.patch.object(
            self.mod, "resolve_checked_repo_root",
            return_value=("/repo/mismatch", v),
        ), mock.patch.object(
            self.mod.cc_invoke, "cc_invoke", return_value=fake_result,
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rc = self.mod._reap_native(dry_run=True, commit_prefix="")

        self.assertEqual(rc, 0)
        self.assertIn(v["message"], stderr.getvalue())

    def test_native_unresolved_with_no_root_never_refuses(self):
        v = _verdict("UNRESOLVED", None, sid=None)
        with mock.patch.object(
            self.mod, "resolve_checked_repo_root",
            return_value=(None, v),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rc = self.mod._reap_native(dry_run=True, commit_prefix="")

        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
