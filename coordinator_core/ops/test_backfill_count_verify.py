"""Tests for coordinator_core.ops.backfill_count_verify (real tmp git repos)."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import backfill_count_verify as bcv
from coordinator_core.ops.test_workday_complete_backfill_scan import _commit_on, _git, _make_repo

BLOCK = "## {day} — all-machines\n\n**Branch:** all refs\n**Commits:** {n} (range: aaaa..bbbb)\n**Backfilled:** yes\n"


@pytest.fixture
def repo(tmp_path_factory) -> Path:
    r = _make_repo(tmp_path_factory)
    for i in range(3):
        _commit_on(r, "2026-11-09", f"a{i}")
    _commit_on(r, "2026-11-10", "b0")
    (r / "state" / "week-changelog").mkdir(parents=True)
    return r


def _write_day(repo: Path, day: str, body: str) -> Path:
    p = repo / "state" / "week-changelog" / f"{day}.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_wrong_block_count_listed_with_stated_and_derived(repo, capsys):
    _write_day(repo, "2026-11-09", BLOCK.format(day="2026-11-09", n=12))
    rc = bcv.main(["--repo", str(repo)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "2026-11-09.md" in out and "stated=12" in out and "derived=3" in out


def test_correct_count_not_listed(repo, capsys):
    _write_day(repo, "2026-11-09", BLOCK.format(day="2026-11-09", n=3))
    _write_day(repo, "2026-11-10", BLOCK.format(day="2026-11-10", n=1))
    rc = bcv.main(["--repo", str(repo)])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_block_in_extra_changelog_file_checked(repo):
    (repo / "CHANGELOG.md").write_text(
        BLOCK.format(day="2026-11-09", n=3) + "\n" + BLOCK.format(day="2026-11-10", n=9), encoding="utf-8"
    )
    mismatches, checked = bcv.verify(str(repo), ["CHANGELOG.md"])
    assert checked == 2
    assert [(m.claim.kind, m.claim.day, m.claim.stated, m.derived) for m in mismatches] == [
        ("block", "2026-11-10", 9, 1)
    ]


def test_headerless_day_file_checked_by_filename_date(repo):
    _write_day(repo, "2026-11-09", "**Commits:** 12 (range: a..b)\n")
    mismatches, _ = bcv.verify(str(repo))
    assert [(m.claim.kind, m.claim.stated, m.derived) for m in mismatches] == [("day_file", 12, 3)]


def test_day_without_commits_derives_zero(repo):
    _write_day(repo, "2026-11-08", BLOCK.format(day="2026-11-08", n=4))
    mismatches, _ = bcv.verify(str(repo))
    assert [(m.claim.day, m.derived) for m in mismatches] == [("2026-11-08", 0)]


def test_work_branch_commits_unioned_and_deduped(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "work/x/y", base)
    _commit_on(repo, "2026-11-10", "w0")
    _git(repo, "checkout", "-q", "main")
    _write_day(repo, "2026-11-10", BLOCK.format(day="2026-11-10", n=2))
    mismatches, checked = bcv.verify(str(repo))
    assert checked == 1 and mismatches == []


def test_header_and_missing_dir_are_ignored(tmp_path):
    (tmp_path / "state" / "week-changelog").mkdir(parents=True)
    (tmp_path / "state" / "week-changelog" / "HEADER.md").write_text("**Commits:** 5\n")
    assert bcv.verify(str(tmp_path)) == ([], 0)
    assert bcv.main(["--repo", str(tmp_path / "nope")]) == 2


def test_git_spawns_independent_of_day_count(repo, monkeypatch):
    import coordinator_core.ops.workday_complete_backfill_scan as scan

    calls = []
    real = scan._run_git

    def counting(args, cwd=None):
        calls.append(args)
        return real(args, cwd=cwd)

    monkeypatch.setattr(scan, "_run_git", counting)
    for d in ("2026-11-05", "2026-11-06", "2026-11-07", "2026-11-09", "2026-11-10"):
        _write_day(repo, d, BLOCK.format(day=d, n=99))
    mismatches, checked = bcv.verify(str(repo))
    assert checked == 5 and len(mismatches) == 5
    assert len(calls) == 2
