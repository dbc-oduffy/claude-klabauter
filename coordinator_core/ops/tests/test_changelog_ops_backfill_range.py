"""changelog.backfill_gaps(from=, to=) backfills every missing day across a multi-week gap and writes each week's WEEK-SUMMARY.md."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import changelog_ops

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

TODAY = "2026-03-20"


def _git(repo, *args, stamp=None) -> None:
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


def _commit(repo, name, stamp) -> None:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"add {name}", stamp=stamp)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "a.txt", "2026-02-25T12:00:00")
    _commit(root, "b.txt", "2026-03-03T12:00:00")
    _commit(root, "c.txt", "2026-03-11T12:00:00")
    _commit(root, "d.txt", "2026-03-18T12:00:00")
    return root


def _call(checkout, **params):
    return asyncio.run(
        changelog_ops._backfill_gaps_handler(
            {"today": TODAY, **params}, repo_root=checkout / ".git"
        )
    )


def _archive(repo) -> list[str]:
    root = repo / "archive" / "week-changelogs"
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*.md")) if root.exists() else []


def test_one_call_fills_every_gap_day_and_each_week_summary_then_is_idempotent(repo) -> None:
    result = _call(repo, **{"from": "2026-02-23", "to": "2026-03-15"})
    assert "error" not in result
    assert _archive(repo) == [
        "2026-02-23/2026-02-25.md",
        "2026-02-23/WEEK-SUMMARY.md",
        "2026-03-02/2026-03-03.md",
        "2026-03-02/WEEK-SUMMARY.md",
        "2026-03-09/2026-03-11.md",
        "2026-03-09/WEEK-SUMMARY.md",
    ]
    assert len(result["backfilled"]) == 3 and len(result["week_summaries"]) == 3
    summary = (repo / "archive/week-changelogs/2026-03-02/WEEK-SUMMARY.md").read_text(encoding="utf-8")
    assert "week_start: 2026-03-02" in summary and "- **2026-03-03**" in summary

    snapshot = {p: p.read_bytes() for p in (repo / "archive").rglob("*.md")}
    again = _call(repo, **{"from": "2026-02-23", "to": "2026-03-15"})
    assert again["backfilled"] == [] and again["week_summaries"] == []
    assert {p: p.read_bytes() for p in (repo / "archive").rglob("*.md")} == snapshot


def test_to_bounds_the_range_like_through(repo) -> None:
    result = _call(repo, **{"from": "2026-02-23", "to": "2026-03-04"})
    assert sorted(Path(p).name for p in result["backfilled"]) == ["2026-02-25.md", "2026-03-03.md"]
    assert not (repo / "archive/week-changelogs/2026-03-09").exists()


def test_dry_run_lists_the_writes_and_touches_nothing(repo) -> None:
    result = _call(repo, **{"from": "2026-03-01", "dry_run": True})
    assert result["dry_run"] is True
    assert sorted(Path(p).name for p in result["backfilled"]) == [
        "2026-03-03.md", "2026-03-11.md", "2026-03-18.md",
    ]
    assert sorted(Path(p).name for p in result["week_summaries"]) == ["WEEK-SUMMARY.md"] * 3
    assert _archive(repo) == []


def test_an_existing_daily_file_is_never_rewritten(repo) -> None:
    week_dir = repo / "archive/week-changelogs/2026-03-09"
    week_dir.mkdir(parents=True)
    (week_dir / "2026-03-11.md").write_text("curated\n", encoding="utf-8")
    result = _call(repo, **{"from": "2026-03-09", "to": "2026-03-15"})
    assert (week_dir / "2026-03-11.md").read_text(encoding="utf-8") == "curated\n"
    assert result["backfilled"] == []
    assert "- **2026-03-11**" in (week_dir / "WEEK-SUMMARY.md").read_text(encoding="utf-8")


def test_an_existing_summary_is_refreshed_with_only_the_new_day(repo) -> None:
    week_dir = repo / "archive/week-changelogs/2026-03-09"
    week_dir.mkdir(parents=True)
    (week_dir / "WEEK-SUMMARY.md").write_text("# kept narrative\n", encoding="utf-8")
    _call(repo, **{"from": "2026-03-09", "to": "2026-03-15"})
    text = (week_dir / "WEEK-SUMMARY.md").read_text(encoding="utf-8")
    assert text.startswith("# kept narrative\n") and "- **2026-03-11**" in text


def test_one_git_log_spawn_covers_the_whole_range(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    real = changelog_ops._run_git_log
    monkeypatch.setattr(
        changelog_ops, "_run_git_log", lambda wt, args: calls.append(args) or real(wt, args)
    )
    _call(repo, **{"from": "2026-02-23", "to": "2026-03-15"})
    assert len(calls) == 1


def test_repo_param_targets_another_checkout(repo, tmp_path) -> None:
    other = tmp_path / "caller"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    result = asyncio.run(
        changelog_ops._backfill_gaps_handler(
            {"today": TODAY, "from": "2026-03-16", "repo": str(repo)}, repo_root=other / ".git"
        )
    )
    assert [Path(p).name for p in result["backfilled"]] == ["2026-03-18.md"]
    assert not (other / "archive").exists()


def test_without_from_the_default_path_is_unchanged(repo) -> None:
    result = _call(repo, host="h")
    assert result["backfilled"] == [] and result["message"] == "no HEADER.md"
    assert _archive(repo) == []


def test_bad_inputs_refuse_before_any_write(repo) -> None:
    assert "'from'" in _call(repo, **{"from": "2026-13-01"})["error"]
    assert "'through'" in _call(repo, **{"from": "2026-03-01", "to": "nope"})["error"]
    assert "error" in _call(repo, **{"from": "2000-01-01"})
    assert "error" in _call(repo, **{"from": "2026-03-01", "repo": "no-such-repo-key"})
    assert _archive(repo) == []
