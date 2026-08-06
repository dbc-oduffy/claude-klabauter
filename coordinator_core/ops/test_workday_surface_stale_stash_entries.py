"""
Tests for coordinator_core.ops.workday_surface_stale_stash_entries — AC5 of
the unscoped-stash-peer-sweep-data-loss spinoff.

Covers: no stashes (silent), fresh stashes only (silent), stale stashes
present (reported with age/ref/subject), the threshold-day boundary, and a
malformed/unparseable `git stash list` line (must not crash — degrades
quietly, matching the sibling malformed-line rule used by
workday_surface_auto_push_failure_stats). All filesystem/git work is
tmp_path-hermetic; stashes are real, created via `git stash push` against a
throwaway repo (never against the shared working tree).
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from coordinator_core.ops import workday_surface_stale_stash_entries as mod


def _git(args, cwd, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "--quiet"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "user.name", "Test"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "README.md"], root)
    _git(["commit", "--quiet", "-m", "seed"], root)
    return root


def _make_stash(repo, filename, content):
    (repo / filename).write_text(content, encoding="utf-8")
    _git(["add", filename], repo)
    _git(["stash", "push", "--quiet", "-u", "--", filename], repo)


def test_no_stashes_is_silent(repo):
    result = mod.surface_stale_stash_entries(str(repo))
    assert result == {
        "threshold_days": 7,
        "total": 0,
        "stale": [],
        "advice": mod.ADVICE,
        "error": None,
    }


def test_fresh_stashes_only_reports_total_but_no_stale_entries(repo):
    _make_stash(repo, "a.txt", "fresh work\n")
    result = mod.surface_stale_stash_entries(str(repo), threshold_days=7)
    assert result["total"] == 1
    assert result["stale"] == []
    assert result["error"] is None


def test_stale_stash_present_is_reported(repo):
    _make_stash(repo, "b.txt", "abandoned work\n")

    old_ts = int((datetime.now(timezone.utc) - timedelta(days=10)).timestamp())
    with patch.object(mod, "_run_stash_list") as run:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"stash@{{0}}\x1f{old_ts}\x1fOn main: abandoned work\n",
            stderr="",
        )
        result = mod.surface_stale_stash_entries(str(repo), threshold_days=7)

    assert result["total"] == 1
    assert len(result["stale"]) == 1
    entry = result["stale"][0]
    assert entry["ref"] == "stash@{0}"
    assert entry["age_days"] >= 10
    assert entry["subject"] == "On main: abandoned work"
    assert result["advice"] == mod.ADVICE


def test_threshold_boundary_just_under_and_just_over(repo):
    now = datetime.now(timezone.utc)
    just_under = int((now - timedelta(days=7) + timedelta(seconds=60)).timestamp())
    just_over = int((now - timedelta(days=7) - timedelta(seconds=60)).timestamp())
    stdout = (
        f"stash@{{0}}\x1f{just_under}\x1fOn main: not yet stale\n"
        f"stash@{{1}}\x1f{just_over}\x1fOn main: just stale\n"
    )
    with patch.object(mod, "_run_stash_list") as run:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=stdout, stderr=""
        )
        result = mod.surface_stale_stash_entries(str(repo), threshold_days=7)

    assert result["total"] == 2
    refs = {entry["ref"] for entry in result["stale"]}
    assert refs == {"stash@{1}"}


def test_malformed_line_does_not_crash_and_is_excluded(repo):
    old_ts = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
    stdout = (
        "not a stash line at all\n"
        f"stash@{{0}}\x1fnot-a-timestamp\x1fOn main: bad timestamp\n"
        f"stash@{{1}}\x1f{old_ts}\x1fOn main: genuinely stale\n"
    )
    with patch.object(mod, "_run_stash_list") as run:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=stdout, stderr=""
        )
        result = mod.surface_stale_stash_entries(str(repo), threshold_days=7)

    assert result["total"] == 3
    assert len(result["stale"]) == 1
    assert result["stale"][0]["ref"] == "stash@{1}"
    assert result["error"] is None


def test_non_repo_repo_root_degrades_quietly_never_raises(tmp_path):
    non_repo = tmp_path / "not-a-git-repo"
    non_repo.mkdir()
    result = mod.surface_stale_stash_entries(str(non_repo))
    assert result["total"] == 0
    assert result["stale"] == []
    assert result["error"] is not None


def test_missing_repo_root_raises_structured_error(tmp_path):
    with pytest.raises(mod.StaleStashEntriesError, match="repo_root"):
        mod.surface_stale_stash_entries(str(tmp_path / "no-such-repo"))


def test_default_threshold_is_seven_days(repo):
    result = mod.surface_stale_stash_entries(str(repo))
    assert result["threshold_days"] == 7
