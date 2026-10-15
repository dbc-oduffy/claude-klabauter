"""changelog-confirm rule over a throwaway repo with dated commits and block files."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workweek_complete.release import changelog_rule

pytestmark = pytest.mark.spawns_process

OK_ROWS = [
    {"id": "backfill", "status": "ran", "exit_code": 0},
]


def _git(root: Path, *args: str, when: str | None = None) -> str:
    env = dict(os.environ)
    if when:
        env["GIT_COMMITTER_DATE"] = when
        env["GIT_AUTHOR_DATE"] = when
    r = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True, env=env, check=True,
        **no_console_creationflags(),
    )
    return r.stdout.strip()


def _repo(tmp_path: Path, days: list[str], blocks: list[str]) -> Path:
    _git_init = subprocess.run(["git", "init", "-q", str(tmp_path)], capture_output=True, **no_console_creationflags())
    assert _git_init.returncode == 0
    wc = tmp_path / "state" / "week-changelog"
    wc.mkdir(parents=True)
    (wc / "HEADER.md").write_text("# Week\n\n**Week starting:** 2026-10-05\n", encoding="utf-8")
    for d in days:
        (tmp_path / f"f-{d}.txt").write_text(d, encoding="utf-8")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-q", "-m", d, when=f"{d}T12:00:00+00:00")
    for name in blocks:
        (wc / name).write_text("block\n", encoding="utf-8")
    return tmp_path


def test_all_covered_passes(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["2026-10-05", "2026-10-06"], ["2026-10-05-h.md", "2026-10-06-h-backfill.md"])
    res = changelog_rule.evaluate(root, ledger_rows=OK_ROWS)
    assert res.passed, res
    assert res.evidence["commit_count"] == 2
    assert res.evidence["block_count"] == 2
    assert res.evidence["week_start"] == "2026-10-05"
    assert res.evidence["entries"] == []


def test_uncovered_commit_misses_and_names_it(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["2026-10-05", "2026-10-06"], ["2026-10-05-h.md"])
    sha = _git(root, "rev-parse", "HEAD")
    res = changelog_rule.evaluate(root, ledger_rows=OK_ROWS)
    assert not res.passed and res.reason == "uncovered-commits"
    assert res.evidence["uncovered"] == [sha]
    assert res.evidence["uncovered_total"] == 1


def test_block_outside_week_misses(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["2026-10-05"], ["2026-10-05.md", "2026-10-20-h.md"])
    res = changelog_rule.evaluate(root, ledger_rows=OK_ROWS)
    assert not res.passed and res.reason == "block-outside-week"
    assert res.evidence["dates"] == ["2026-10-20"]


def test_missing_backfill_row_misses(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["2026-10-05"], ["2026-10-05.md"])
    res = changelog_rule.evaluate(root, ledger_rows=[])
    assert not res.passed and res.reason == "backfill-not-run"


def test_passes_with_no_lint_row(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["2026-10-05"], ["2026-10-05.md"])
    res = changelog_rule.evaluate(root, ledger_rows=OK_ROWS)
    assert res.passed, res


def test_pending_release_file_is_not_a_daily_block(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["2026-10-05"], ["2026-10-05.md", "2026-10-30-pending-release.md"])
    res = changelog_rule.evaluate(root, ledger_rows=OK_ROWS)
    assert res.passed, res
    assert res.evidence["block_count"] == 1


def test_pending_release_alone_does_not_cover_a_commit(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["2026-10-05"], ["2026-10-05-pending-release.md"])
    res = changelog_rule.evaluate(root, ledger_rows=OK_ROWS)
    assert not res.passed and res.reason == "uncovered-commits"
