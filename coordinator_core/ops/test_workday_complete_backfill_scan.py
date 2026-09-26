"""
Pytest for coordinator_core.ops.workday_complete_backfill_scan.

Port of: workday-complete-backfill-scan.test.sh (DoE 3a561713, 2026-07-22) —
mirrors the load-bearing subset of that bash fixture suite. This pytest exercises
the SAME logic paths natively in-process via `main()` so the port has its own
fast, claude-klabauter-resident regression net.

De-machined 2026-07-19 (docs/plans/2026-07-19-de-machine-backfill-scan-per-day.md
§ C2): the per-machine apparatus (TM1 per-machine-row, TM5 reconcile-only
exclusivity suppression, TM7/TM9 merge-before-wrap escape hatch, TD1/TD2
dangling-defer) was retired wholesale in lockstep with the module rewrite —
those tests asserted on row/stderr shapes (`<day>\\t<machine>\\t...`,
`NO-EXCLUSIVE-WORK`, `DANGLING-DEFER`) that no longer exist. This file now
covers the per-day predicate (AC1/AC2), the full-day union span (AC3/DEC-3),
and the DEC-5 semantic-shift case explicitly.

Spec backlink: DoE-claude:pln-bash-polyglot-clean-slate-full-5c71ee
Spec backlink: pln-de-machine-workday-complete-ba-f1b7e6 § C2
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import List, Optional

import pytest

from coordinator_core.ops.workday_complete_backfill_scan import main
from coordinator_core.win_portability import no_console_creationflags

# own oracle-parity contract requires this. The spawn ratchet's `_BASELINE` is
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

# which git's date parser interprets in the INVOKING PROCESS's local system timezone
os.environ["TZ"] = "UTC"
if hasattr(time, "tzset"):
    time.tzset()


def _git(repo: Path, *args: str, env: Optional[dict] = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=20,
        stdin=subprocess.DEVNULL,
        env=env,
    **no_console_creationflags())
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


def _make_repo(tmp_path_factory) -> Path:
    repo = tmp_path_factory.mktemp("backfill-repo")
    _git(repo, "init", "-q")
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "archive" / "daily-summaries").mkdir(parents=True)
    return repo


def _commit_on(repo: Path, day: str, msg: str, fname: Optional[str] = None, time_: str = "12:00:00Z") -> str:
    fname = fname or f"f-{day}-{msg.replace(' ', '_')}.txt"
    (repo / fname).write_text(msg + "\n")
    _git(repo, "add", "--", fname)
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = f"{day}T{time_}"
    env["GIT_COMMITTER_DATE"] = f"{day}T{time_}"
    result = subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", msg],
        capture_output=True,
        text=True,
        timeout=20,
        stdin=subprocess.DEVNULL,
        env=env,
    **no_console_creationflags())
    assert result.returncode == 0, result.stderr
    return _git(repo, "rev-parse", "HEAD")


def _branch_commit_from(repo: Path, branch: str, start_sha: str, day: str, time_: str, msg: str, fname: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", branch, start_sha, "-q"],
        capture_output=True,
        text=True,
        timeout=20,
        stdin=subprocess.DEVNULL,
    **no_console_creationflags())
    if result.returncode != 0:
        _git(repo, "checkout", branch, "-q")
    (repo / fname).write_text(msg + "\n")
    _git(repo, "add", "--", fname)
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = f"{day}T{time_}"
    env["GIT_COMMITTER_DATE"] = f"{day}T{time_}"
    result = subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", msg],
        capture_output=True,
        text=True,
        timeout=20,
        stdin=subprocess.DEVNULL,
        env=env,
    **no_console_creationflags())
    assert result.returncode == 0, result.stderr
    return _git(repo, "rev-parse", "HEAD")


def _run_scan(repo: Path, monkeypatch, capsys, lookback: int = 14, today: str = "2026-03-15",
              extra_env: Optional[dict] = None) -> int:
    monkeypatch.setenv("COORDINATOR_ROOT", str(repo))
    monkeypatch.setenv("COORDINATOR_ROOT_WARN_SUPPRESS", "1")
    if extra_env:
        for k, v in extra_env.items():
            monkeypatch.setenv(k, v)
    rc = main(["--lookback", str(lookback), "--today", today])
    return rc


@pytest.fixture
def repo(tmp_path_factory):
    return _make_repo(tmp_path_factory)


def test_commit_no_summary_emitted_and_summary_present_excluded(repo, monkeypatch, capsys):
    _commit_on(repo, "2026-02-20", "old (out of lookback)")
    _commit_on(repo, "2026-03-10", "missed day A")
    _commit_on(repo, "2026-03-10", "missed day A second commit")
    _commit_on(repo, "2026-03-12", "covered day")
    (repo / "archive" / "daily-summaries" / "2026-03-12.md").write_text("summary\n")
    (repo / "state" / "week-changelog").mkdir(parents=True)
    (repo / "state" / "week-changelog" / "2026-03-12.md").write_text("changelog\n")

    rc = _run_scan(repo, monkeypatch, capsys)
    out = capsys.readouterr().out

    assert rc == 0
    assert "\n2026-03-10\t" in "\n" + out
    assert "2026-03-12\t" not in out
    assert "2026-03-11\t" not in out
    assert "2026-02-20\t" not in out

    row = next(ln for ln in out.splitlines() if ln.startswith("2026-03-10\t"))
    fields = row.split("\t")
    assert len(fields) == 5
    assert fields[1] == "2"
    assert len(fields[4].split(",")) == int(fields[1])
    assert fields[2] != fields[3]
    assert len(fields[2]) >= 7


def test_bad_lookback_rejected(repo, monkeypatch, capsys):
    _commit_on(repo, "2026-03-10", "x")
    monkeypatch.setenv("COORDINATOR_ROOT", str(repo))
    rc = main(["--lookback", "0", "--today", "2026-03-15"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "positive integer" in err


def test_lookback_signed_value_rejected(repo, monkeypatch, capsys):
    monkeypatch.setenv("COORDINATOR_ROOT", str(repo))
    rc = main(["--lookback", "+5", "--today", "2026-03-15"])
    assert rc == 1
    assert "got '+5'" in capsys.readouterr().err


def test_bad_today_rejected(repo, monkeypatch, capsys):
    monkeypatch.setenv("COORDINATOR_ROOT", str(repo))
    rc = main(["--lookback", "5", "--today", "not-a-date"])
    assert rc == 1
    assert "got 'not-a-date'" in capsys.readouterr().err


def test_today_invalid_calendar_date_rejected(repo, monkeypatch, capsys):
    monkeypatch.setenv("COORDINATOR_ROOT", str(repo))
    rc = main(["--lookback", "5", "--today", "2026-13-40"])
    assert rc == 1
    assert "got '2026-13-40'" in capsys.readouterr().err


def test_today_valid_date_accepted(repo, monkeypatch, capsys):
    _commit_on(repo, "2026-03-14", "missed day for valid --today test")
    rc = _run_scan(repo, monkeypatch, capsys, lookback=1)
    out = capsys.readouterr().out
    assert rc == 0
    assert "2026-03-14\t" in out


def test_date_minus_end_to_end(repo, monkeypatch, capsys):
    _commit_on(repo, "2026-03-14", "missed day for date-minus test")
    rc = _run_scan(repo, monkeypatch, capsys, lookback=1)
    out = capsys.readouterr().out
    assert rc == 0
    assert "2026-03-14\t" in out


def test_ac1_one_uncovered_day_emits_exactly_one_row(repo, monkeypatch, capsys):
    _commit_on(repo, "2026-03-10", "solo missed-day commit")

    rc = _run_scan(repo, monkeypatch, capsys, lookback=1, today="2026-03-11")
    out = capsys.readouterr().out

    assert rc == 0
    lines = [ln for ln in out.splitlines() if ln]
    assert len(lines) == 1
    fields = lines[0].split("\t")
    assert len(fields) == 5
    day, count, base, tip, shas = fields
    assert day == "2026-03-10"
    assert count == "1"
    assert len(base) >= 7 and len(tip) >= 7
    assert shas.split(",") == [tip]
    assert fields[2] == fields[3]


@pytest.mark.parametrize(
    "changelog_fname,summary_fname",
    [
        ("2026-03-10.md", "2026-03-10.md"),
        ("2026-03-10-somemachine.md", "2026-03-10-somemachine.md"),
    ],
)
def test_ac2_transition_tolerant_coverage_suppresses_row(repo, monkeypatch, capsys, changelog_fname, summary_fname):
    _commit_on(repo, "2026-03-10", "covered-by-either-shape commit")
    (repo / "archive" / "daily-summaries" / summary_fname).write_text("covered\n")
    week_changelog_dir = repo / "state" / "week-changelog"
    week_changelog_dir.mkdir(parents=True)
    (week_changelog_dir / changelog_fname).write_text("covered\n")

    rc = _run_scan(repo, monkeypatch, capsys, lookback=1, today="2026-03-11")
    out = capsys.readouterr().out

    assert rc == 0
    assert "2026-03-10\t" not in out


def test_ac2_summary_only_no_longer_suppresses_row(repo, monkeypatch, capsys):
    _commit_on(repo, "2026-03-10", "summary written before changelog step ran")
    (repo / "archive" / "daily-summaries" / "2026-03-10.md").write_text("summary only\n")

    rc = _run_scan(repo, monkeypatch, capsys, lookback=1, today="2026-03-11")
    out = capsys.readouterr().out

    assert rc == 0
    assert "2026-03-10\t" in out


def test_ac2_changelog_only_dangling_link_no_longer_suppresses_row(repo, monkeypatch, capsys):
    _commit_on(repo, "2026-03-10", "changelog block written, summary never written")
    week_changelog_dir = repo / "state" / "week-changelog"
    week_changelog_dir.mkdir(parents=True)
    (week_changelog_dir / "2026-03-10.md").write_text(
        "## 2026-03-10 — some-machine\n\n"
        "**Links:** archive/daily-summaries/2026-03-10-some-machine.md (never written)\n"
    )

    rc = _run_scan(repo, monkeypatch, capsys, lookback=1, today="2026-03-11")
    out = capsys.readouterr().out

    assert rc == 0
    assert "2026-03-10\t" in out


def _write_synthesized_stub(path, day: str, host: str = "some-machine") -> None:
    path.write_text(
        f"## {day} — {host} (synthesized backfill)\n"
        "\n"
        "**Commits:** 1 (oldest: abc1234, newest: abc1234)\n"
        "**Scope:** (synthesized — daily ceremony skipped, no human-curated narrative)\n"
        "\n"
        "### Commit log\n"
        "\n"
        "```\n"
        "abc1234 a commit\n"
        "```\n"
    )


def test_synthesized_backfill_stub_does_not_suppress_row(repo, monkeypatch, capsys):
    _commit_on(repo, "2026-03-10", "day only covered by a synthesized stub")
    (repo / "archive" / "daily-summaries" / "2026-03-10.md").write_text("summary\n")
    week_changelog_dir = repo / "state" / "week-changelog"
    week_changelog_dir.mkdir(parents=True)
    _write_synthesized_stub(week_changelog_dir / "2026-03-10-some-machine-backfill.md", "2026-03-10")

    rc = _run_scan(repo, monkeypatch, capsys, lookback=1, today="2026-03-11")
    captured = capsys.readouterr()

    assert rc == 0
    assert "2026-03-10\t" in captured.out
    assert "synthesized backfill stub" in captured.err
    assert "2026-03-10-some-machine-backfill.md" in captured.err


def test_archived_synthesized_backfill_stub_does_not_suppress_row(repo, monkeypatch, capsys):
    _commit_on(repo, "2026-03-10", "day only covered by an archived synthesized stub")
    (repo / "archive" / "daily-summaries" / "2026-03-10.md").write_text("summary\n")
    archived_week = repo / "archive" / "week-changelogs" / "2026-03-09"
    archived_week.mkdir(parents=True)
    _write_synthesized_stub(archived_week / "2026-03-10-some-machine-backfill.md", "2026-03-10")

    rc = _run_scan(repo, monkeypatch, capsys, lookback=1, today="2026-03-11")
    out = capsys.readouterr().out

    assert rc == 0
    assert "2026-03-10\t" in out


def test_synthesized_backfill_stub_alongside_real_block_still_covers(repo, monkeypatch, capsys):
    _commit_on(repo, "2026-03-10", "day with both a stub and a real block")
    (repo / "archive" / "daily-summaries" / "2026-03-10.md").write_text("summary\n")
    week_changelog_dir = repo / "state" / "week-changelog"
    week_changelog_dir.mkdir(parents=True)
    _write_synthesized_stub(week_changelog_dir / "2026-03-10-some-machine-backfill.md", "2026-03-10")
    (week_changelog_dir / "2026-03-10.md").write_text("## 2026-03-10 — real curated narrative\n")

    rc = _run_scan(repo, monkeypatch, capsys, lookback=1, today="2026-03-11")
    out = capsys.readouterr().out

    assert rc == 0
    assert "2026-03-10\t" not in out


def test_archived_week_changelog_block_still_counts_as_covered(repo, monkeypatch, capsys):
    _commit_on(repo, "2026-03-10", "day whose week has since been archived")
    (repo / "archive" / "daily-summaries" / "2026-03-10.md").write_text("summary\n")
    archived_week = repo / "archive" / "week-changelogs" / "2026-03-09"
    archived_week.mkdir(parents=True)
    (archived_week / "2026-03-10.md").write_text("archived block\n")

    rc = _run_scan(repo, monkeypatch, capsys, lookback=1, today="2026-03-11")
    out = capsys.readouterr().out

    assert rc == 0
    assert "2026-03-10\t" not in out


def test_archived_week_changelog_block_alone_still_reports_gap(repo, monkeypatch, capsys):
    """The archive lookup is a live-vs-archived LOCATION tolerance for one artifact
    type, never a re-entry point for the retired cross-artifact-type OR: an
    archived changelog block with no daily summary is still a gap."""
    _commit_on(repo, "2026-03-10", "archived block, summary never written")
    archived_week = repo / "archive" / "week-changelogs" / "2026-03-09"
    archived_week.mkdir(parents=True)
    (archived_week / "2026-03-10.md").write_text("archived block\n")

    rc = _run_scan(repo, monkeypatch, capsys, lookback=1, today="2026-03-11")
    out = capsys.readouterr().out

    assert rc == 0
    assert "2026-03-10\t" in out


def test_state_root_unresolvable_with_commits_reports_gap_even_if_summary_present(repo, monkeypatch, capsys):
    """`state_root=None` (unresolvable seam, see `_resolve_state_root_seam`) must
    fail toward flagging a gap, not toward silently treating the changelog side as
    satisfied. Simulated here by pointing COORDINATOR_ROOT at the repo but exercising
    `_day_covered` directly with state_root=None, mirroring what the seam returns
    when it cannot resolve."""
    from coordinator_core.ops.workday_complete_backfill_scan import _day_covered

    (repo / "archive" / "daily-summaries" / "2026-03-10.md").write_text("summary\n")
    assert _day_covered(str(repo), None, "2026-03-10") is False


def test_ac3_full_day_union_span_picks_global_base_and_tip(repo, monkeypatch, capsys):
    day = "2026-03-14"

    root_sha = _commit_on(repo, "2026-03-12", "shared root", fname="root.txt")

    m1_early = _branch_commit_from(repo, f"work/m1/{day}", root_sha, day, "08:00:00Z", "m1 early", "m1.txt")

    _git(repo, "checkout", "main", "-q")
    main_mid = _commit_on(repo, day, "main mid-day commit", fname="main-mid.txt", time_="12:00:00Z")

    m2_late = _branch_commit_from(repo, f"work/m2/{day}", root_sha, day, "20:00:00Z", "m2 late", "m2.txt")

    rc = _run_scan(repo, monkeypatch, capsys, lookback=1, today="2026-03-15")
    out = capsys.readouterr().out

    assert rc == 0
    lines = [ln for ln in out.splitlines() if ln.startswith(f"{day}\t")]
    assert len(lines) == 1
    fields = lines[0].split("\t")
    assert len(fields) == 5
    _, count, base, tip, shas = fields

    assert count == "3"
    assert base == root_sha
    assert tip == m2_late
    assert tip != m1_early
    assert tip != main_mid

    assert shas.split(",") == [m1_early, main_mid, m2_late]
    assert len(shas.split(",")) == int(count)


def test_ac3_the_sha_column_names_commits_the_base_tip_range_does_not_contain(
    repo, monkeypatch, capsys
):
    day = "2026-03-16"
    root_sha = _commit_on(repo, "2026-03-13", "shared root", fname="rootc.txt")
    m1_early = _branch_commit_from(repo, f"work/m1/{day}", root_sha, day, "08:00:00Z", "m1 early", "c1.txt")
    _git(repo, "checkout", "main", "-q")
    main_mid = _commit_on(repo, day, "main mid", fname="c2.txt", time_="12:00:00Z")
    m2_late = _branch_commit_from(repo, f"work/m2/{day}", root_sha, day, "20:00:00Z", "m2 late", "c3.txt")

    rc = _run_scan(repo, monkeypatch, capsys, lookback=1, today="2026-03-17")
    out = capsys.readouterr().out
    assert rc == 0

    line = next(ln for ln in out.splitlines() if ln.startswith(f"{day}\t"))
    _, count, base, tip, shas = line.split("\t")

    in_range = _git(repo, "rev-list", f"{base}..{tip}").split()
    assert len(in_range) == 1 and in_range[0] == m2_late
    assert int(count) == 3
    assert len(in_range) != int(count)

    union = shas.split(",")
    assert sorted(union) == sorted([m1_early, main_mid, m2_late])
    assert len(union) == int(count)
    assert m1_early not in in_range and main_mid not in in_range


def test_empty_output_when_all_covered(repo, monkeypatch, capsys):
    _commit_on(repo, "2026-03-13", "covered")
    (repo / "archive" / "daily-summaries" / "2026-03-13.md").write_text("s\n")
    (repo / "state" / "week-changelog").mkdir(parents=True)
    (repo / "state" / "week-changelog" / "2026-03-13.md").write_text("c\n")
    rc = _run_scan(repo, monkeypatch, capsys)
    out = capsys.readouterr().out
    assert rc == 0
    assert out == ""


def test_empty_output_when_no_commit_window(repo, monkeypatch, capsys):
    rc = _run_scan(repo, monkeypatch, capsys, lookback=3, today="2026-03-11")
    out = capsys.readouterr().out
    assert rc == 0
    assert out == ""


# even if they belong to one machine (M1) and a DIFFERENT machine (M2) has
# INTENTIONAL semantic shift from the pre-de-machining behavior: retiring
# per-machine EXCLUSIVITY ATTRIBUTION wholesale (2026-07-19 PM ruling) means


def test_dec5_any_block_covers_the_day_even_with_older_unrecorded_peer_work(repo, monkeypatch, capsys):
    day = "2026-03-20"
    root_sha = _commit_on(repo, "2026-03-18", "shared root", fname="root2.txt")

    _branch_commit_from(repo, f"work/m2/{day}", root_sha, day, "08:00:00Z", "m2 older unrecorded work", "m2older.txt")

    (repo / "archive" / "daily-summaries" / f"{day}.md").write_text("m1's summary\n")
    week_changelog_dir = repo / "state" / "week-changelog"
    week_changelog_dir.mkdir(parents=True)
    (week_changelog_dir / f"{day}.md").write_text("m1's block\n")

    rc = _run_scan(repo, monkeypatch, capsys, lookback=1, today="2026-03-21")
    out = capsys.readouterr().out

    assert rc == 0
    assert f"{day}\t" not in out
