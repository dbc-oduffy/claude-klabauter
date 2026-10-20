"""backfill_gaps(through=...) bounds the loop; its default today is the coordinator local day."""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core.ops import changelog_ops


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A fake common_dir whose worktree holds a HEADER.md anchored 2026-03-02."""
    git_dir = tmp_path / "repo" / ".git"
    git_dir.mkdir(parents=True)
    wc = tmp_path / "repo" / "state" / "week-changelog"
    wc.mkdir(parents=True)
    (wc / "HEADER.md").write_text("**Week starting:** 2026-03-02\n", encoding="utf-8")
    monkeypatch.setattr(changelog_ops, "_git_log_for_date", lambda wt, d, n: f"abc commit {d}")
    monkeypatch.setattr(changelog_ops, "declare_write", lambda p: None)
    return git_dir, wc


def _dates(result: dict) -> list[str]:
    return sorted(p.rsplit("2026-", 1)[1][:5] for p in result["backfilled"])


def test_through_stops_the_loop_at_that_day(repo):
    git_dir, _ = repo
    result = changelog_ops.backfill_gaps(
        repo_root=git_dir, host="h", today_override="2026-03-06", through="2026-03-04"
    )
    assert _dates(result) == ["03-02", "03-03", "03-04"]


def test_through_after_today_is_bounded_by_today(repo):
    git_dir, _ = repo
    result = changelog_ops.backfill_gaps(
        repo_root=git_dir, host="h", today_override="2026-03-03", through="2026-03-09"
    )
    assert _dates(result) == ["03-02", "03-03"]


def test_no_through_keeps_today_bound(repo):
    git_dir, _ = repo
    result = changelog_ops.backfill_gaps(repo_root=git_dir, host="h", today_override="2026-03-04")
    assert _dates(result) == ["03-02", "03-03", "03-04"]


def test_default_today_is_the_coordinator_local_day_not_utc(repo, monkeypatch):
    git_dir, _ = repo
    monkeypatch.setattr(changelog_ops, "local_day", lambda root=None: "2026-03-03")
    monkeypatch.setattr(changelog_ops, "_today_utc", lambda: "2026-03-30")
    result = changelog_ops.backfill_gaps(repo_root=git_dir, host="h")
    assert _dates(result) == ["03-02", "03-03"]


@pytest.mark.parametrize("bad", ["2026-3-4", "9999-99-99", "../x", "2026-02-30"])
def test_handler_refuses_malformed_through(repo, bad):
    git_dir, wc = repo
    result = asyncio.run(
        changelog_ops._backfill_gaps_handler({"host": "h", "through": bad}, repo_root=git_dir)
    )
    assert result["backfilled"] == [] and "'through' param" in result["error"]
    assert not list(wc.glob("*-backfill.md"))


def test_handler_passes_through(repo):
    git_dir, _ = repo
    result = asyncio.run(
        changelog_ops._backfill_gaps_handler(
            {"host": "h", "today": "2026-03-06", "through": "2026-03-03"}, repo_root=git_dir
        )
    )
    assert _dates(result) == ["03-02", "03-03"]


def test_cli_malformed_through_exits_2_without_running(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(changelog_ops, "backfill_gaps", lambda **kw: called.append(kw))
    assert changelog_ops.main(["--through", "nope"]) == 2
    assert not called
    assert "through" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--through", "2026-03-03"], ["--through=2026-03-03"]])
def test_cli_forwards_through(monkeypatch, argv, tmp_path):
    seen = {}
    monkeypatch.setattr(changelog_ops, "git_common_dir", lambda cwd: str(tmp_path / ".git"))
    monkeypatch.setattr(changelog_ops, "compute_machine", lambda: "h")
    monkeypatch.setattr(
        changelog_ops, "backfill_gaps", lambda **kw: seen.update(kw) or {"backfilled": [], "skipped": []}
    )
    assert changelog_ops.main(argv) == 0
    assert seen["through"] == "2026-03-03"
