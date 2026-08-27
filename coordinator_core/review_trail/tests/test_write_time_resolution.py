"""
coordinator_core.review_trail.tests.test_write_time_resolution

Purpose: pins C1b's writer-side wiring — `write_review_trail_entry` folds
its own newly-written record into the reviewed_set store
(`coordinator_core.review_trail.reviewed_set`) via
`coordinator_core.review_trail.backfill.resolve_and_fold`, applying
`coverage.py`'s five preserved credit rules, WITHOUT ever failing the write
itself on a resolution error.

Spec backlink: docs/plans/2026-08-27-the-reviewed-set-is-a-file-not-a-computation.md § C1b
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

from coordinator_core.ops.review_trail_write import write_review_trail_entry
from coordinator_core.review_trail import backfill as review_trail_backfill
from coordinator_core.review_trail import reviewed_set as rs


def _git(args, cwd):
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), capture_output=True, encoding="utf-8", check=True,
    )


def _init_repo(path):
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)


def _make_commit(repo, message, session_id=None) -> str:
    body = message if session_id is None else f"{message}\n\nSession-Id: {session_id}"
    _git(["commit", "--allow-empty", "-m", body], repo)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, encoding="utf-8", check=True,
    ).stdout.strip()


class TestWriteTimeFoldIn:
    def test_ok_diff_record_folds_into_reviewed_set_store(self, tmp_path, monkeypatch):
        for var in (
            "REVIEW_TRAIL_OUTPUT_ROOT", "COORDINATOR_REVIEW_WORKSTREAM",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
        ):
            monkeypatch.delenv(var, raising=False)
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base = _make_commit(repo, "base", session_id="sess-abcdef01")
        tip = _make_commit(repo, "tip", session_id="sess-abcdef01")

        write_review_trail_entry(
            sha_range=f"{base}..{tip}",
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=10,
            session_id="sess-abcdef01",
            workstream=None,
            caller_worktree=repo,
        )

        reviewed = rs.read_reviewed_set(str(repo))
        assert tip in reviewed, "the tip commit must be credited in the reviewed-set store"

    def test_pending_verdict_record_is_excluded_not_folded(self, tmp_path, monkeypatch):
        for var in (
            "REVIEW_TRAIL_OUTPUT_ROOT", "COORDINATOR_REVIEW_WORKSTREAM",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
        ):
            monkeypatch.delenv(var, raising=False)
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base = _make_commit(repo, "base", session_id="sess-abcdef02")
        tip = _make_commit(repo, "tip", session_id="sess-abcdef02")

        write_review_trail_entry(
            sha_range=f"{base}..{tip}",
            reviewer="code-reviewer",
            scope="chain",
            verdict="pending",
            diff_loc=10,
            session_id="sess-abcdef02",
            workstream=None,
            caller_worktree=repo,
        )

        reviewed = rs.read_reviewed_set(str(repo))
        assert tip not in reviewed, "verdict=pending must never be credited (rule 1)"

    def test_write_time_resolution_failure_never_fails_the_write(self, tmp_path, monkeypatch):
        """A resolution-path exception must be swallowed — the record write
        itself must still succeed and return normally."""
        for var in (
            "REVIEW_TRAIL_OUTPUT_ROOT", "COORDINATOR_REVIEW_WORKSTREAM",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
        ):
            monkeypatch.delenv(var, raising=False)
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base = _make_commit(repo, "base", session_id="sess-abcdef03")
        tip = _make_commit(repo, "tip", session_id="sess-abcdef03")

        def _boom(*args, **kwargs):
            raise RuntimeError("synthetic resolution failure")

        monkeypatch.setattr(review_trail_backfill, "resolve_and_fold", _boom)

        result = write_review_trail_entry(
            sha_range=f"{base}..{tip}",
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=10,
            session_id="sess-abcdef03",
            workstream=None,
            caller_worktree=repo,
        )

        assert Path(result["out_path"]).is_file(), (
            "the trail record file must exist even when write-time reviewed-set "
            "resolution raises"
        )

    def test_no_caller_worktree_skips_resolution_without_raising(self, tmp_path, monkeypatch):
        """The REVIEW_TRAIL_OUTPUT_ROOT test-isolation path (no caller_worktree)
        has no repo to fold against — resolution must simply be skipped, never
        attempted against a nonexistent worktree."""
        monkeypatch.delenv("COORDINATOR_REVIEW_WORKSTREAM", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-abcdef04")
        out_root = tmp_path / "iso"
        out_root.mkdir()
        monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(out_root))

        result = write_review_trail_entry(
            sha_range="abc1234..def5678",
            reviewer="code-reviewer",
            scope="chain",
            verdict="ok",
            diff_loc=10,
            workstream=None,
            caller_worktree=None,
        )
        assert Path(result["out_path"]).is_file()
