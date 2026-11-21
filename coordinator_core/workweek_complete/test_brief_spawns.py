"""coordinator_core/workweek_complete/test_brief.py — C4a pin: Step 2's
directive resolves through the validate gate CLI, not the standalone
resolver.

Spec backlink: docs/plans/2026-07-30-diff-scoped-ceremony-gates-elegant.md
(C4, Design decision 2 / Problem 3). The repoint itself and CONSUMES_MANIFEST
membership were already landed by a prior session on this branch (verified on
disk: `d_step2_resolve_validation_cmd` already carries
`cli="validate-fast-and-packageability"`, `args=["fast"]`, and
`coordinator-resolve-validation-cmd` is not, and has never needed to be, a
CONSUMES_MANIFEST member -- no other directive ever named it). This file adds
the row's own declared test surface (`coordinator_core/workweek_complete/
test_brief.py`) as a focused, file-local pin, alongside the pre-existing
broader pin in `test_workweek_complete_contract.py::
test_step2_directive_names_the_validate_gate_cli_fast_subcommand`.
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import date, datetime
from pathlib import Path

import pytest

from coordinator_core.cadence.period_mode import week_window
from coordinator_core.workweek_complete import brief as wwc_brief

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

backfill_scan = wwc_brief.backfill_scan


_CONCLUDING = (
    "d_step13_archive_close",
    "d_step13_5_post_command_hook",
    "jp_step8_5_loe_high_water",
    "jp_step9_editorial_bucketing",
    "jp_step9_pm_release_notes_gate",
    "jp_step10_semver_judgment",
    "jp_step10_5_gh_release_publish",
)


def _brief_at(
    monkeypatch, tmp_path: Path, anchor: str | None, now: datetime, **flags
) -> tuple[int, dict]:
    from coordinator_core.workweek_complete.brief_inputs import BriefInputs

    if anchor is not None:
        header = tmp_path / "state" / "week-changelog" / "HEADER.md"
        header.parent.mkdir(parents=True, exist_ok=True)
        header.write_text(f"**Week starting:** {anchor}\n", encoding="utf-8")
    monkeypatch.setattr(
        wwc_brief,
        "compute_brief_inputs",
        lambda *a, **k: BriefInputs(repo_root=tmp_path.as_posix()),
    )
    monkeypatch.setattr(wwc_brief, "resolve_operator_config", lambda env=None: None)
    return wwc_brief.brief(now_local=now, **flags)


@pytest.fixture(autouse=True)
def _no_git_scan(monkeypatch) -> None:
    from coordinator_core.ops.workday_complete_backfill_scan import GapScan

    monkeypatch.setattr(wwc_brief.backfill_scan, "scan", lambda root, *a, **k: GapScan())


_REAL_GAP_SCAN = backfill_scan.scan


def _git(repo: Path, *args: str, when: str | None = None) -> None:
    env = dict(os.environ)
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _covered_week_two(repo: Path) -> None:
    """Week 2026-10-05: day fully recorded (changelog block, daily summary, WEEK-SUMMARY)."""
    week = repo / "archive" / "week-changelogs" / "2026-10-05"
    week.mkdir(parents=True)
    (week / "2026-10-08-h.md").write_text("block\n", encoding="utf-8")
    (week / "WEEK-SUMMARY.md").write_text("summary\n", encoding="utf-8")
    daily = repo / "archive" / "daily-summaries"
    daily.mkdir(parents=True)
    (daily / "2026-10-08-h.md").write_text("summary\n", encoding="utf-8")


@pytest.fixture
def gap_repo(monkeypatch, tmp_path: Path) -> Path:
    """A real git repo with commits on 2026-10-01 (week 09-28) and 2026-10-08 (week 10-05), no HEADER.md."""
    monkeypatch.setenv("TZ", "UTC")
    if hasattr(time, "tzset"):
        time.tzset()
    monkeypatch.setattr(wwc_brief.backfill_scan, "scan", _REAL_GAP_SCAN)
    monkeypatch.setattr(wwc_brief.backfill_scan, "_coordinator_local_day", lambda: "2026-10-15")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "symbolic-ref", "HEAD", "refs/heads/main")
    for key, val in (("user.email", "t@t.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(tmp_path, "config", key, val)
    for day in ("2026-10-01", "2026-10-08"):
        (tmp_path / f"f-{day}.txt").write_text(day, encoding="utf-8")
        _git(tmp_path, "add", "--", f"f-{day}.txt")
        _git(tmp_path, "commit", "-q", "-m", day, when=f"{day}T12:00:00Z")
    _covered_week_two(tmp_path)
    yield tmp_path
    if hasattr(time, "tzset"):
        monkeypatch.undo()
        time.tzset()


def _gap_scan_weeks(repo: Path) -> list[str]:
    result = _REAL_GAP_SCAN(str(repo), today="2026-10-15")
    days = [date.fromisoformat(d) for d in [*(r.date for r in result.rows), *result.older_gaps]]
    return sorted({week_window(d, date(2026, 9, 28))[0].isoformat() for d in days})


@pytest.mark.spawns_process
@pytest.mark.parametrize("header_anchor", [None, "2026-09-28"])
def test_missed_equals_the_daily_gap_scan_weeks_with_or_without_header(
    monkeypatch, gap_repo: Path, header_anchor
) -> None:
    expected = _gap_scan_weeks(gap_repo)
    assert expected == ["2026-09-28"]
    _, env = _brief_at(monkeypatch, gap_repo, header_anchor, datetime(2026, 10, 15, 10, 0))
    assert env["period_mode"]["missed"] == expected
    assert "2026-10-05" not in env["period_mode"]["missed"]
    assert any(
        d["id"] == "d_step13_catch_up_archive_2026-09-28" for d in env["directives"]
    ) == (env["period_mode"]["mode"] == "catch_up")


@pytest.mark.spawns_process
def test_week_with_a_week_summary_is_never_missed(monkeypatch, gap_repo: Path) -> None:
    week = gap_repo / "archive" / "week-changelogs" / "2026-09-28"
    week.mkdir(parents=True)
    (week / "WEEK-SUMMARY.md").write_text("summary\n", encoding="utf-8")
    assert _gap_scan_weeks(gap_repo) == ["2026-09-28"]
    _, env = _brief_at(monkeypatch, gap_repo, None, datetime(2026, 10, 15, 10, 0))
    assert env["period_mode"]["missed"] == []
