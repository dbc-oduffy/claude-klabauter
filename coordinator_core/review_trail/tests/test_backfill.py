"""
coordinator_core.review_trail.tests.test_backfill

Purpose: pins `coordinator_core.review_trail.backfill` — the one-shot,
idempotent, resumable pass folding already-on-disk `state/review-trail/
*.json` records into the reviewed_set store, and the shared
`resolve_and_fold` path it shares with write-time resolution
(`test_write_time_resolution.py`).

Spec backlink: docs/plans/2026-08-27-the-reviewed-set-is-a-file-not-a-computation.md § C1b
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

from coordinator_core.review_trail import backfill
from coordinator_core.review_trail import reviewed_set as rs
from coordinator_core.win_portability import (
    no_console_creationflags,
    no_console_passthrough_kwargs,
)


def _git(args, cwd):
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), capture_output=True, encoding="utf-8", check=True,
    **no_console_creationflags(),
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
    **no_console_creationflags(),
).stdout.strip()


def _write_trail_record(repo: Path, filename: str, record: dict) -> Path:
    trail_dir = repo / "state" / "review-trail"
    trail_dir.mkdir(parents=True, exist_ok=True)
    path = trail_dir / filename
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


class TestRunBackfill:
    def test_folds_a_creditable_diff_record(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base = _make_commit(repo, "base")
        tip = _make_commit(repo, "tip")
        _write_trail_record(
            repo,
            "2026-01-01-000000-abcdef01.json",
            {
                "sha_range": f"{base}..{tip}",
                "reviewer": "code-reviewer",
                "scope": "chain",
                "scope_kind": "diff",
                "verdict": "ok",
                "diff_loc": 5,
                "session_id": "abcdef01",
                "workstream": None,
            },
        )

        result = backfill.run_backfill(str(repo))

        assert len(result.folded) == 1
        assert tip in rs.read_reviewed_set(str(repo))

    def test_pending_verdict_is_excluded_not_folded(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base = _make_commit(repo, "base")
        tip = _make_commit(repo, "tip")
        _write_trail_record(
            repo,
            "2026-01-01-000001-abcdef02.json",
            {
                "sha_range": f"{base}..{tip}",
                "reviewer": "code-reviewer",
                "scope": "chain",
                "scope_kind": "diff",
                "verdict": "pending",
                "diff_loc": 5,
                "session_id": "abcdef02",
                "workstream": None,
            },
        )

        result = backfill.run_backfill(str(repo))

        assert len(result.excluded) == 1
        assert tip not in rs.read_reviewed_set(str(repo))

    def test_integration_scope_kind_is_excluded(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base = _make_commit(repo, "base")
        tip = _make_commit(repo, "tip")
        _write_trail_record(
            repo,
            "2026-01-01-000002-abcdef03.json",
            {
                "sha_range": f"{base}..{tip}",
                "reviewer": "code-reviewer",
                "scope": "chain",
                "scope_kind": "integration",
                "verdict": "ok",
                "diff_loc": 5,
                "session_id": "abcdef03",
                "workstream": None,
            },
        )

        result = backfill.run_backfill(str(repo))

        assert len(result.excluded) == 1
        assert tip not in rs.read_reviewed_set(str(repo))

    def test_stored_head_record_is_excluded(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base = _make_commit(repo, "base")
        tip = _make_commit(repo, "tip")
        _write_trail_record(
            repo,
            "2026-01-01-000003-abcdef04.json",
            {
                "sha_range": f"{base}..HEAD",
                "reviewer": "code-reviewer",
                "scope": "chain",
                "scope_kind": "diff",
                "verdict": "ok",
                "diff_loc": 5,
                "session_id": "abcdef04",
                "workstream": None,
            },
        )

        result = backfill.run_backfill(str(repo))

        assert len(result.excluded) == 1
        assert tip not in rs.read_reviewed_set(str(repo))

    def test_idempotent_second_call_folds_nothing_new(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base = _make_commit(repo, "base")
        tip = _make_commit(repo, "tip")
        _write_trail_record(
            repo,
            "2026-01-01-000004-abcdef05.json",
            {
                "sha_range": f"{base}..{tip}",
                "reviewer": "code-reviewer",
                "scope": "chain",
                "scope_kind": "diff",
                "verdict": "ok",
                "diff_loc": 5,
                "session_id": "abcdef05",
                "workstream": None,
            },
        )

        first = backfill.run_backfill(str(repo))
        second = backfill.run_backfill(str(repo))

        assert len(first.folded) == 1
        assert second.folded == []
        assert second.excluded == []
        assert second.unresolved == []

    def test_resumable_after_unresolved_endpoint_heals_on_retry(self, tmp_path):
        """An abbreviated/unresolvable endpoint leaves the record UNRESOLVED
        on the first pass; once resolvable it folds on the next call — the
        same self-healing shape a crash between the two store writes needs."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base = _make_commit(repo, "base")
        tip = _make_commit(repo, "tip")
        _write_trail_record(
            repo,
            "2026-01-01-000005-abcdef06.json",
            {
                "sha_range": f"{base}..deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                "reviewer": "code-reviewer",
                "scope": "chain",
                "scope_kind": "diff",
                "verdict": "ok",
                "diff_loc": 5,
                "session_id": "abcdef06",
                "workstream": None,
            },
        )

        first = backfill.run_backfill(str(repo))
        assert len(first.unresolved) == 1
        assert tip not in rs.read_reviewed_set(str(repo))

        # Second call over the SAME unresolvable record must retry, not skip
        # (its id was never marked folded).
        second = backfill.run_backfill(str(repo))
        assert len(second.unresolved) == 1

    def test_plan_kind_credits_only_planning_artifact_commits(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base = _make_commit(repo, "base")
        (repo / "docs").mkdir()
        (repo / "docs" / "plans").mkdir()
        (repo / "docs" / "plans" / "x.md").write_text("plan", encoding="utf-8")
        _git(["add", "-A"], repo)
        plan_tip = _make_commit(repo, "plan commit")

        (repo / "code.py").write_text("x = 1\n", encoding="utf-8")
        _git(["add", "-A"], repo)
        code_tip = _make_commit(repo, "code commit")

        _write_trail_record(
            repo,
            "2026-01-01-000006-abcdef07.json",
            {
                "sha_range": f"{base}..{code_tip}",
                "reviewer": "code-reviewer",
                "scope": "chain",
                "scope_kind": "plan",
                "verdict": "ok",
                "diff_loc": 5,
                "session_id": "abcdef07",
                "workstream": None,
            },
        )

        result = backfill.run_backfill(str(repo))

        assert len(result.folded) == 1
        reviewed = rs.read_reviewed_set(str(repo))
        assert plan_tip in reviewed, "the planning-artifact commit must be credited"
        assert code_tip not in reviewed, (
            "a scope_kind='plan' record must never credit a code commit (rule 3)"
        )

    def test_no_trail_dir_returns_empty_result(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)

        result = backfill.run_backfill(str(repo))

        assert result.folded == []
        assert result.excluded == []
        assert result.unresolved == []

    def test_unparseable_file_is_reported_not_dropped(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        trail_dir = repo / "state" / "review-trail"
        trail_dir.mkdir(parents=True)
        bad_path = trail_dir / "2026-08-28-000000-deadbeef.json"
        bad_path.write_text("not json at all {{{", encoding="utf-8")

        result = backfill.run_backfill(str(repo))

        assert result.parse_failures == ["state/review-trail/2026-08-28-000000-deadbeef.json"]
        assert result.folded == []
        assert result.excluded == []
        assert result.unresolved == []
