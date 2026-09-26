from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

import pytest

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_LIB_DIR = os.path.join(_BIN_DIR, "lib")
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import records_query  # noqa: E402

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _verdict(verdict: str, resolved_root, sid="sess-x", session_root=None):
    return {
        "verdict": verdict,
        "session_root": session_root,
        "resolved_root": resolved_root,
        "sid": sid,
        "message": f"repo-identity (checked resolver): fake {verdict} for test",
    }


class TestResolveRepoRootExplicitRootThreading(unittest.TestCase):

    def test_explicit_root_forwarded_to_checked_resolver(self):
        with mock.patch.object(
            records_query,
            "resolve_checked_repo_root",
            return_value=("/repo/pinned", _verdict("EXPLICIT", "/repo/pinned", sid=None)),
        ) as mocked:
            root = records_query._resolve_repo_root(explicit_root="/repo/pinned")

        mocked.assert_called_once_with(explicit_root="/repo/pinned")
        self.assertEqual(root, "/repo/pinned")

    def test_omitted_explicit_root_preserves_existing_resolution(self):
        with mock.patch.object(
            records_query,
            "resolve_checked_repo_root",
            return_value=("/repo/match", _verdict("MATCH", "/repo/match")),
        ) as mocked:
            root = records_query._resolve_repo_root()

        mocked.assert_called_once_with(explicit_root=None)
        self.assertEqual(root, "/repo/match")


class TestQueryRecordsExplicitRootThreading(unittest.TestCase):

    def test_query_records_forwards_explicit_root(self):
        with mock.patch.object(
            records_query, "_resolve_repo_root", return_value="/repo/from-arg"
        ) as mocked_resolve, mock.patch.object(
            records_query, "route_mutation", return_value={"records": ""}
        ) as mocked_route:
            records_query.query_records(
                "handoff", "", explicit_root="/repo/from-arg"
            )

        mocked_resolve.assert_called_once_with(explicit_root="/repo/from-arg")
        self.assertEqual(mocked_route.call_args[0][2], "/repo/from-arg")

    def test_query_records_omitted_explicit_root_defaults_to_none(self):
        with mock.patch.object(
            records_query, "_resolve_repo_root", return_value="/repo/default"
        ) as mocked_resolve, mock.patch.object(
            records_query, "route_mutation", return_value={"records": ""}
        ):
            records_query.query_records("handoff", "")

        mocked_resolve.assert_called_once_with(explicit_root=None)


def _load_triage_module():
    path = os.path.join(_BIN_DIR, "workday-start-handoff-triage.py")
    spec = importlib.util.spec_from_file_location(
        "workday_start_handoff_triage_scan_scope_under_test", path
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _git(repo_dir, *args):
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr}")
    return proc.stdout


def _init_repo(repo_dir):
    _git(repo_dir, "init", "-q")
    _git(repo_dir, "config", "user.email", "test@example.com")
    _git(repo_dir, "config", "user.name", "Test")


def _write_and_commit(repo_dir, rel_path, content, message):
    full = repo_dir / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    _git(repo_dir, "add", rel_path)
    _git(repo_dir, "commit", "-q", "-m", message)


class TestGitLastCommitEpochsBatchCwdThreading(unittest.TestCase):

    def test_cwd_param_scopes_git_to_the_given_repo(self):
        import tempfile

        mod = _load_triage_module()
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as other_td:
            import pathlib

            repo_dir = pathlib.Path(td)
            _init_repo(repo_dir)
            plans_dir = repo_dir / "docs" / "plans"
            _write_and_commit(repo_dir, "docs/plans/a.md", "---\nstatus: executing\n---\n", "add a")

            # Process cwd is a DIFFERENT, non-git directory -- without cwd
            prior_cwd = os.getcwd()
            os.chdir(other_td)
            try:
                result_without_cwd = mod._git_last_commit_epochs_batch(
                    [plans_dir / "a.md"]
                )
                result_with_cwd = mod._git_last_commit_epochs_batch(
                    [plans_dir / "a.md"], cwd=str(repo_dir)
                )
            finally:
                os.chdir(prior_cwd)

        self.assertIsNone(result_without_cwd[plans_dir / "a.md"])
        self.assertIsNotNone(result_with_cwd[plans_dir / "a.md"])

    def test_omitted_cwd_preserves_process_cwd_resolution(self):
        import tempfile

        mod = _load_triage_module()
        with tempfile.TemporaryDirectory() as td:
            import pathlib

            repo_dir = pathlib.Path(td)
            _init_repo(repo_dir)
            plans_dir = repo_dir / "docs" / "plans"
            _write_and_commit(repo_dir, "docs/plans/a.md", "---\nstatus: executing\n---\n", "add a")

            prior_cwd = os.getcwd()
            os.chdir(repo_dir)
            try:
                result = mod._git_last_commit_epochs_batch([plans_dir / "a.md"])
            finally:
                os.chdir(prior_cwd)

        self.assertIsNotNone(result[plans_dir / "a.md"])


if __name__ == "__main__":
    unittest.main()
