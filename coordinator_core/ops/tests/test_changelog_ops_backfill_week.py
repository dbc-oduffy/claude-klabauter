"""backfill_gaps(week=...) composes archive/week-changelogs/<week>/<day>.md from all refs, header-free, writing nothing."""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from coordinator_core.ops import changelog_ops

WEEK = "2026-03-02"  # a Monday


def _git(repo, *args, stamp=None):
    env = dict(os.environ)
    if stamp:
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _commit(repo, name, stamp):
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"add {name}", stamp=stamp)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "base.txt", "2026-02-20T12:00:00")
    _git(root, "checkout", "-q", "-b", "side")
    _commit(root, "side.txt", "2026-03-03T12:00:00")
    _git(root, "checkout", "-q", "main")
    _commit(root, "main.txt", "2026-03-05T12:00:00")
    return root


def _compose(repo, **kw):
    return changelog_ops.backfill_gaps(
        repo_root=repo / ".git", host="h", today_override="2026-03-08", week=WEEK, **kw
    )


def _days(composed):
    return sorted(p.name for p, _ in composed)


def _tree_files(repo):
    return sorted(
        str(p.relative_to(repo)) for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts
    )


def test_week_composes_each_commit_day_across_all_refs_without_header_or_write(repo):
    before = _tree_files(repo)
    composed = _compose(repo)
    assert _days(composed) == ["2026-03-03.md", "2026-03-05.md"]
    path, text = composed[0]
    assert path == repo / "archive" / "week-changelogs" / WEEK / "2026-03-03.md"
    assert text.startswith("## 2026-03-03 — all-machines\n")
    assert "**Commits:** 1" in text and "**Backfilled:** yes" in text
    assert _tree_files(repo) == before
    assert not (repo / "archive").exists()


def test_default_path_still_ignores_side_branch_and_needs_header(repo):
    result = changelog_ops.backfill_gaps(
        repo_root=repo / ".git", host="h", today_override="2026-03-08"
    )
    assert result["message"] == "no HEADER.md" and result["backfilled"] == []


def test_existing_day_file_is_omitted(repo):
    week_dir = repo / "archive" / "week-changelogs" / WEEK
    week_dir.mkdir(parents=True)
    (week_dir / "2026-03-03-old.md").write_text("curated", encoding="utf-8")
    assert _days(_compose(repo)) == ["2026-03-05.md"]
    assert (week_dir / "2026-03-03-old.md").read_text(encoding="utf-8") == "curated"


def test_through_bounds_the_week(repo):
    assert _days(_compose(repo, through="2026-03-04")) == ["2026-03-03.md"]


def test_one_git_spawn_for_the_whole_week(repo, monkeypatch):
    calls = []
    real = changelog_ops._run_git_log
    monkeypatch.setattr(
        changelog_ops, "_run_git_log", lambda wt, args: calls.append(args) or real(wt, args)
    )
    _compose(repo)
    assert len(calls) == 1
    assert "--all" in calls[0] and "--no-merges" in calls[0]


@pytest.mark.parametrize("bad", ["2026-03-03", "2026-3-2", "../x", "2026-02-30"])
def test_handler_refuses_non_monday_or_malformed_week(repo, bad):
    result = asyncio.run(
        changelog_ops._backfill_gaps_handler({"host": "h", "week": bad}, repo_root=repo / ".git")
    )
    assert result["backfilled"] == [] and "'week' param" in result["error"]
    assert not (repo / "archive").exists()


def test_handler_returns_composed_and_writes_nothing(repo):
    result = asyncio.run(
        changelog_ops._backfill_gaps_handler(
            {"host": "h", "today": "2026-03-08", "week": WEEK}, repo_root=repo / ".git"
        )
    )
    assert sorted(os.path.basename(c["path"]) for c in result["composed"]) == [
        "2026-03-03.md",
        "2026-03-05.md",
    ]
    assert result["backfilled"] == []
    assert not (repo / "archive").exists()
