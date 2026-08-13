"""test_checked_repo_resolver_records_query.py -- C2 test surface for
`coordinator/bin/lib/records_query.py::_resolve_repo_root`'s repoint onto
the C1 checked resolver.

Spec backlink: pln-one-checked-resolver-for-the-c-035d59
§ C2 / AC2-AC5, AC10.

This module is C2's own (no other chunk in this wave writes to it -- see
the plan's "Dispatch shape" note; C1's own resolver-internals test lives at
`test_checked_repo_resolver.py` and is left untouched).

C1's own module already exhaustively covers `resolve_checked_repo_root`'s
verdict-construction machinery against real on-disk registry fixtures
(`test_checked_repo_resolver.py`) -- duplicating that harness here would
test C1's code, not C2's. What C2 actually authored is the CALL SITE:
`records_query._resolve_repo_root`'s branch on the returned verdict. These
tests mock `records_query.resolve_checked_repo_root` directly (the name
bound into `records_query`'s own namespace by its `from repo_identity
import resolve_checked_repo_root` line) and assert on `_resolve_repo_root`'s
own behavior per verdict -- MATCH/EXPLICIT proceed silently, MISMATCH warns
to stderr and still proceeds with the resolved root (READER classification,
AC10, DR-277), UNRESOLVED never refuses and falls back to `os.getcwd()`
only when the resolver returns no root at all.
"""

from __future__ import annotations

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

import cli_shared  # noqa: E402
import git_hook_install  # noqa: E402
import records_query  # noqa: E402
import repo_identity  # noqa: E402


def _verdict(verdict: str, resolved_root, sid="sess-x", session_root=None):
    return {
        "verdict": verdict,
        "session_root": session_root,
        "resolved_root": resolved_root,
        "sid": sid,
        "message": f"repo-identity (checked resolver): fake {verdict} for test",
    }


class TestResolveRepoRootBranching(unittest.TestCase):
    def test_match_returns_resolved_root_silently(self):
        with mock.patch.object(
            records_query,
            "resolve_checked_repo_root",
            return_value=("/repo/match", _verdict("MATCH", "/repo/match")),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                root = records_query._resolve_repo_root()

        self.assertEqual(root, "/repo/match")
        self.assertEqual(stderr.getvalue(), "")

    def test_explicit_returns_resolved_root_silently(self):
        with mock.patch.object(
            records_query,
            "resolve_checked_repo_root",
            return_value=("/repo/explicit", _verdict("EXPLICIT", "/repo/explicit", sid=None)),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                root = records_query._resolve_repo_root()

        self.assertEqual(root, "/repo/explicit")
        self.assertEqual(stderr.getvalue(), "")

    def test_mismatch_warns_and_still_proceeds_with_resolved_root(self):
        """AC10 (READER classification): a MISMATCH is advisory, never a
        refusal -- the resolved root is still returned, but a warning
        naming the divergence is written to stderr using the resolver's
        own pre-rendered message."""
        v = _verdict("MISMATCH", "/repo/mismatch")
        with mock.patch.object(
            records_query,
            "resolve_checked_repo_root",
            return_value=("/repo/mismatch", v),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                root = records_query._resolve_repo_root()

        self.assertEqual(root, "/repo/mismatch")
        self.assertIn(v["message"], stderr.getvalue())

    def test_unresolved_with_a_root_never_refuses_and_proceeds(self):
        """UNRESOLVED must never be hardened into a refusal (AC4, DR-277) --
        when the resolver still hands back a best-effort root (e.g. no sid
        in env, but a real git root was found), that root is used as-is."""
        v = _verdict("UNRESOLVED", "/repo/unresolved", sid=None)
        with mock.patch.object(
            records_query,
            "resolve_checked_repo_root",
            return_value=("/repo/unresolved", v),
        ):
            root = records_query._resolve_repo_root()

        self.assertEqual(root, "/repo/unresolved")

    def test_unresolved_with_no_root_falls_back_to_cwd(self):
        """No git root at all -- the checked resolver returns `None` --
        must degrade to `os.getcwd()`, exactly mirroring the predecessor's
        git-failure branch (never raise, never refuse)."""
        v = _verdict("UNRESOLVED", None, sid=None)
        with mock.patch.object(
            records_query,
            "resolve_checked_repo_root",
            return_value=(None, v),
        ):
            root = records_query._resolve_repo_root()

        self.assertEqual(root, os.getcwd())

    def test_binds_and_branches_on_both_return_values(self):
        """AC5: a call site that binds `(root, verdict)` and never branches
        on `verdict` is a failure this asserts against directly -- MATCH and
        MISMATCH resolve to the identical root here, so only a real branch
        on `verdict["verdict"]` (not a root-value side effect) explains the
        stderr divergence between the two calls."""
        same_root = "/repo/same"

        with mock.patch.object(
            records_query,
            "resolve_checked_repo_root",
            return_value=(same_root, _verdict("MATCH", same_root)),
        ):
            stderr_match = io.StringIO()
            with redirect_stderr(stderr_match):
                root_match = records_query._resolve_repo_root()

        mismatch_verdict = _verdict("MISMATCH", same_root)
        with mock.patch.object(
            records_query,
            "resolve_checked_repo_root",
            return_value=(same_root, mismatch_verdict),
        ):
            stderr_mismatch = io.StringIO()
            with redirect_stderr(stderr_mismatch):
                root_mismatch = records_query._resolve_repo_root()

        self.assertEqual(root_match, same_root)
        self.assertEqual(root_mismatch, same_root)
        self.assertEqual(stderr_match.getvalue(), "")
        self.assertIn(mismatch_verdict["message"], stderr_mismatch.getvalue())


class TestResolveFromRepoBranching(unittest.TestCase):
    """`cli_shared.resolve_from_repo`'s own bind-and-branch shape. Mocks
    `cli_shared.resolve_checked_repo_root` (the name bound into
    `cli_shared`'s own namespace by its `from repo_identity import
    resolve_checked_repo_root` line) plus the machine-local registry reads
    it fans out to, so these assert only the branch this call site
    authored -- not C1's resolver internals (that's
    `test_checked_repo_resolver.py`'s job) and not `em_id_for_root`'s own
    resolution ladder (unrelated to this slice)."""

    def test_match_returns_silently(self):
        with mock.patch.object(cli_shared, "machine_local_repos_keys", return_value=[]), \
             mock.patch.object(cli_shared, "machine_local_get", return_value=None), \
             mock.patch.object(
                 cli_shared,
                 "resolve_checked_repo_root",
                 return_value=("/repo/match", _verdict("MATCH", "/repo/match")),
             ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                em_id = cli_shared.resolve_from_repo()

        self.assertEqual(em_id, "match-em")
        self.assertEqual(stderr.getvalue(), "")

    def test_unresolved_never_refuses_and_degrades_to_unknown(self):
        """UNRESOLVED with no root at all must degrade to `root=None`,
        exactly mirroring the predecessor's git-failure branch -- never a
        raise, and `em_id_for_root(None, ...)` names the fallback identity
        `"unknown-sender-em"`."""
        with mock.patch.object(cli_shared, "machine_local_repos_keys", return_value=[]), \
             mock.patch.object(cli_shared, "machine_local_get", return_value=None), \
             mock.patch.object(
                 cli_shared,
                 "resolve_checked_repo_root",
                 return_value=(None, _verdict("UNRESOLVED", None, sid=None)),
             ):
            em_id = cli_shared.resolve_from_repo()

        self.assertEqual(em_id, "unknown-sender-em")

    def test_binds_and_branches_on_both_return_values(self):
        """AC2/AC5: a call site that binds `(root, verdict)` and never
        branches on `verdict` is a failure this asserts against directly --
        MATCH and MISMATCH resolve to the identical root here, so only a
        real branch on `verdict["verdict"]` (not a root-value side effect)
        explains the stderr divergence between the two calls."""
        same_root = "/repo/same"

        with mock.patch.object(cli_shared, "machine_local_repos_keys", return_value=[]), \
             mock.patch.object(cli_shared, "machine_local_get", return_value=None), \
             mock.patch.object(
                 cli_shared,
                 "resolve_checked_repo_root",
                 return_value=(same_root, _verdict("MATCH", same_root)),
             ):
            stderr_match = io.StringIO()
            with redirect_stderr(stderr_match):
                cli_shared.resolve_from_repo()

        mismatch_verdict = _verdict("MISMATCH", same_root)
        with mock.patch.object(cli_shared, "machine_local_repos_keys", return_value=[]), \
             mock.patch.object(cli_shared, "machine_local_get", return_value=None), \
             mock.patch.object(
                 cli_shared,
                 "resolve_checked_repo_root",
                 return_value=(same_root, mismatch_verdict),
             ):
            stderr_mismatch = io.StringIO()
            with redirect_stderr(stderr_mismatch):
                cli_shared.resolve_from_repo()

        self.assertEqual(stderr_match.getvalue(), "")
        self.assertIn(mismatch_verdict["message"], stderr_mismatch.getvalue())


class TestGitRootBranching(unittest.TestCase):
    """`git_hook_install._git_root`'s own bind-and-branch shape. Mocks
    `repo_identity.resolve_checked_repo_root` (`_git_root` does `from
    repo_identity import resolve_checked_repo_root` fresh inside the
    function body, so the name to patch is on the source module, not
    bound into `git_hook_install`'s namespace at import time)."""

    def test_match_returns_root_silently(self):
        with mock.patch.object(
            repo_identity,
            "resolve_checked_repo_root",
            return_value=("/repo/match", _verdict("MATCH", "/repo/match")),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                root = git_hook_install._git_root()

        self.assertEqual(root, "/repo/match")
        self.assertEqual(stderr.getvalue(), "")

    def test_mismatch_warns_and_still_proceeds_with_resolved_root(self):
        v = _verdict("MISMATCH", "/repo/mismatch")
        with mock.patch.object(
            repo_identity,
            "resolve_checked_repo_root",
            return_value=("/repo/mismatch", v),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                root = git_hook_install._git_root()

        self.assertEqual(root, "/repo/mismatch")
        self.assertIn(v["message"], stderr.getvalue())

    def test_unresolved_with_no_root_never_refuses_and_returns_none(self):
        """`_git_root` never raises on UNRESOLVED; with no root at all it
        returns `root or None`, i.e. `None` -- exactly mirroring the
        predecessor's git-failure branch, which a hook installer that
        "must never fail loudly enough to block a commit" depends on."""
        with mock.patch.object(
            repo_identity,
            "resolve_checked_repo_root",
            return_value=(None, _verdict("UNRESOLVED", None, sid=None)),
        ):
            root = git_hook_install._git_root()

        self.assertIsNone(root)

    def test_binds_and_branches_on_both_return_values(self):
        """AC2/AC5: a call site that binds `(root, verdict)` and never
        branches on `verdict` is a failure this asserts against directly --
        MATCH and MISMATCH resolve to the identical root here, so only a
        real branch on `verdict["verdict"]` (not a root-value side effect)
        explains the stderr divergence between the two calls."""
        same_root = "/repo/same"

        with mock.patch.object(
            repo_identity,
            "resolve_checked_repo_root",
            return_value=(same_root, _verdict("MATCH", same_root)),
        ):
            stderr_match = io.StringIO()
            with redirect_stderr(stderr_match):
                root_match = git_hook_install._git_root()

        mismatch_verdict = _verdict("MISMATCH", same_root)
        with mock.patch.object(
            repo_identity,
            "resolve_checked_repo_root",
            return_value=(same_root, mismatch_verdict),
        ):
            stderr_mismatch = io.StringIO()
            with redirect_stderr(stderr_mismatch):
                root_mismatch = git_hook_install._git_root()

        self.assertEqual(root_match, same_root)
        self.assertEqual(root_mismatch, same_root)
        self.assertEqual(stderr_match.getvalue(), "")
        self.assertIn(mismatch_verdict["message"], stderr_mismatch.getvalue())


if __name__ == "__main__":
    unittest.main()
