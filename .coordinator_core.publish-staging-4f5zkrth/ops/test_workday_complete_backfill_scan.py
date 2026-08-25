"""
Pytest for coordinator_core.ops.workday_complete_backfill_scan.

Port of: workday-complete-backfill-scan.test.sh (DoE 3a561713, 2026-07-22) —
mirrors the load-bearing subset of that bash fixture suite. This pytest exercises
the SAME logic paths natively in-process via `main()` so the port has its own
fast, makima-resident regression net.

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

# Declared, not excused: this file's `main()` calls genuinely shell out to
# `git log --after/--before` (see `_window_args` below) to build per-day commit
# windows -- the property under test IS git's own date-window resolution and
# local-timezone interpretation, which no mock stands in for. The module port's
# own oracle-parity contract requires this. The spawn ratchet's `_BASELINE` is
# shrink-only pre-existing residue and is explicitly not the route for this
# file -- coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

# `_window_args` passes bare (offset-less) date-times to `git log --after/--before`,
# which git's date parser interprets in the INVOKING PROCESS's local system timezone
# (documented git behavior) — a faithfully-reproduced oracle seam, not a bug (see the
# module docstring's negative-spec list). Every commit fixture in this file is stamped
# with an explicit `Z` (UTC) offset, so the window computation and the fixture commit
# instants only agree deterministically when the test process itself runs in UTC. Pin
# it here so results are host/CI-timezone-independent regardless of the local operator's
# or CI runner's `TZ`. Confirmed empirically (2026-07-19 review): without this pin,
# `TZ=Pacific/Auckland` flips 5 of 11 tests in this file (window-boundary and
# root-commit-fallback both drift with the host offset).
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
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


def _make_repo(tmp_path_factory) -> Path:
    repo = tmp_path_factory.mktemp("backfill-repo")
    _git(repo, "init", "-q")
    # Pin the default branch to "main" regardless of the host git's
    # init.defaultBranch config — _collect_union_refs sweeps refs/heads/main
    # by literal name, so tests that rely on the union including the default
    # branch (AC3) need a deterministic name.
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
    )
    assert result.returncode == 0, result.stderr
    return _git(repo, "rev-parse", "HEAD")


def _branch_commit_from(repo: Path, branch: str, start_sha: str, day: str, time_: str, msg: str, fname: str) -> str:
    """Create/checkout `branch` at `start_sha`, add one commit dated `day`T`time_`."""
    result = subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", branch, start_sha, "-q"],
        capture_output=True,
        text=True,
        timeout=20,
        stdin=subprocess.DEVNULL,
    )
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
    )
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


# ---------------------------------------------------------------------------
# core per-day scan (global-fallback, single-lineage)
# ---------------------------------------------------------------------------


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
    assert "2026-03-12\t" not in out  # both artifacts present -> excluded
    assert "2026-03-11\t" not in out  # no-commit day absent
    assert "2026-02-20\t" not in out  # beyond lookback

    row = next(ln for ln in out.splitlines() if ln.startswith("2026-03-10\t"))
    fields = row.split("\t")
    assert len(fields) == 4  # <day>\t<count>\t<base>\t<tip>, no machine column
    assert fields[1] == "2"  # commit count
    assert fields[2] != fields[3]  # baseline != tip (root-commit fallback did not fire)
    assert len(fields[2]) >= 7  # well-formed sha


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
    # Regression for the silent-success bug: a malformed --today used to make
    # every `_date_minus` call return "", so the scan loop `continue`d every
    # iteration and the tool exited 0 having scanned zero days. It must now be
    # rejected as a usage error at parse time instead.
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


# ---------------------------------------------------------------------------
# AC1: one uncovered day -> exactly one row
# ---------------------------------------------------------------------------


def test_ac1_one_uncovered_day_emits_exactly_one_row(repo, monkeypatch, capsys):
    _commit_on(repo, "2026-03-10", "solo missed-day commit")

    rc = _run_scan(repo, monkeypatch, capsys, lookback=1, today="2026-03-11")
    out = capsys.readouterr().out

    assert rc == 0
    lines = [ln for ln in out.splitlines() if ln]
    assert len(lines) == 1
    fields = lines[0].split("\t")
    assert len(fields) == 4
    day, count, base, tip = fields
    assert day == "2026-03-10"
    assert count == "1"
    assert len(base) >= 7 and len(tip) >= 7
    # Root-commit-fallback case: the sole commit here IS the repo's root commit,
    # so `git rev-parse {sha}^` fails and `_rev_parse_parent` falls back to `sha`
    # itself — base and tip are the same SHA.
    assert fields[2] == fields[3]


# ---------------------------------------------------------------------------
# AC2: transition-tolerant `<day>*.md` glob (DEC-1) — either the new
# de-machined `<day>.md` shape or the legacy `<day>-<machine>.md` shape is
# accepted for EACH artifact directory independently. As of the 2026-08-06 AND
# fix, BOTH `state/week-changelog/` and `archive/daily-summaries/` must carry
# a matching file for the day to be suppressed — the glob tolerance is
# per-directory filename-shape tolerance, not a substitute for the other
# directory.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changelog_fname,summary_fname",
    [
        ("2026-03-10.md", "2026-03-10.md"),  # new de-machined shape, both sides
        ("2026-03-10-somemachine.md", "2026-03-10-somemachine.md"),  # legacy shape, both sides
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
    """Regression for the 2026-08-06 incident's failure mode 2 (order-sensitivity /
    summary-written-first no-op): a daily-summary file alone, with NO matching
    week-changelog block, must NOT suppress the row -- the changelog block still
    needs to land. Under the old OR this silently suppressed the gap and made
    Step 6 -> Step 6b ordering load-bearing; under AND it is correctly reported."""
    _commit_on(repo, "2026-03-10", "summary written before changelog step ran")
    (repo / "archive" / "daily-summaries" / "2026-03-10.md").write_text("summary only\n")

    rc = _run_scan(repo, monkeypatch, capsys, lookback=1, today="2026-03-11")
    out = capsys.readouterr().out

    assert rc == 0
    assert "2026-03-10\t" in out


def test_ac2_changelog_only_dangling_link_no_longer_suppresses_row(repo, monkeypatch, capsys):
    """Regression for the 2026-08-06 incident's failure mode 1 (dangling-link
    blindness): a week-changelog block alone, whose own `Links:` line may point at
    a daily-summary file that was never written, must NOT suppress the row. Under
    the old OR the changelog block's mere existence was enough and the missing
    summary went undetected; under AND it is correctly reported."""
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


# ---------------------------------------------------------------------------
# Item 3 (2026-08-11 fix, cross-repo/inbox/2026-08-11-project-rag-em-backfill-
# changelog-cli-three-defects.md): a `backfill_gaps()`-synthesized raw-git-log
# stub must NOT satisfy the changelog half of `_day_covered` -- its own
# heading says a human never wrote it.
# ---------------------------------------------------------------------------


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
    """A live `state/week-changelog/<day>-<host>-backfill.md` synthesized stub
    (the exact shape `changelog_ops._compose_backfill_block` writes) must NOT
    be read as changelog coverage even though it matches the `<day>*.md` glob
    -- the day must still be reported as a gap."""
    _commit_on(repo, "2026-03-10", "day only covered by a synthesized stub")
    (repo / "archive" / "daily-summaries" / "2026-03-10.md").write_text("summary\n")
    week_changelog_dir = repo / "state" / "week-changelog"
    week_changelog_dir.mkdir(parents=True)
    _write_synthesized_stub(week_changelog_dir / "2026-03-10-some-machine-backfill.md", "2026-03-10")

    rc = _run_scan(repo, monkeypatch, capsys, lookback=1, today="2026-03-11")
    captured = capsys.readouterr()

    assert rc == 0
    assert "2026-03-10\t" in captured.out
    # Item 3's visibility requirement: the substitution must be reported, not silent.
    assert "synthesized backfill stub" in captured.err
    assert "2026-03-10-some-machine-backfill.md" in captured.err


def test_archived_synthesized_backfill_stub_does_not_suppress_row(repo, monkeypatch, capsys):
    """The same synthesized-stub exclusion applies to the archived location --
    a content marker survives the weekly archive sweep; a filename convention
    would not have."""
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
    """A real, human-curated block for the same day (e.g. after a later
    `/workday-complete --for-date` run lands one alongside a stale synthesized
    stub) still counts as coverage -- the exclusion targets the stub content,
    not the whole directory."""
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
    """A closed week's changelog blocks are swept out of `state/week-changelog/`
    into `archive/week-changelogs/<week-start>/`; only the CURRENT week stays live.
    Checking the live directory alone (the shape the 2026-08-06 AND fix first
    shipped with) flagged every day older than the current week as a permanent
    gap — measured on makima's own tree the same day: 11 of 11 days in a 14-day
    lookback, every one of them carrying both artifacts, just archived."""
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


# ---------------------------------------------------------------------------
# AC3/DEC-3: full-day union span across two work/*/* branches + main picks
# the GLOBAL oldest-parent base and GLOBAL newest tip and de-duplicated
# count — never one lineage's own endpoints.
# ---------------------------------------------------------------------------


def test_ac3_full_day_union_span_picks_global_base_and_tip(repo, monkeypatch, capsys):
    day = "2026-03-14"

    # Shared root, well before the window, on main.
    root_sha = _commit_on(repo, "2026-03-12", "shared root", fname="root.txt")

    # work/m1/<day>: earliest commit of the day (08:00), parented on root_sha.
    # This branch's own tip would be its own 08:00 commit if scanned alone.
    m1_early = _branch_commit_from(repo, f"work/m1/{day}", root_sha, day, "08:00:00Z", "m1 early", "m1.txt")

    # main: mid-day commit (12:00), also parented on root_sha.
    _git(repo, "checkout", "main", "-q")
    main_mid = _commit_on(repo, day, "main mid-day commit", fname="main-mid.txt", time_="12:00:00Z")

    # work/m2/<day>: latest commit of the day (20:00), parented on root_sha.
    # This branch's own base would be root_sha too, but its own tip alone
    # would NOT reflect m1's earlier commit's existence in the count.
    m2_late = _branch_commit_from(repo, f"work/m2/{day}", root_sha, day, "20:00:00Z", "m2 late", "m2.txt")

    rc = _run_scan(repo, monkeypatch, capsys, lookback=1, today="2026-03-15")
    out = capsys.readouterr().out

    assert rc == 0
    lines = [ln for ln in out.splitlines() if ln.startswith(f"{day}\t")]
    assert len(lines) == 1
    fields = lines[0].split("\t")
    assert len(fields) == 4
    _, count, base, tip = fields

    assert count == "3"  # m1_early + main_mid + m2_late, deduped union across refs
    assert base == root_sha  # parent of the GLOBAL oldest commit (m1_early), not m2's own base
    assert tip == m2_late  # GLOBAL newest commit, not main's or m1's own tip
    assert tip != m1_early
    assert tip != main_mid


# ---------------------------------------------------------------------------
# empty output on covered / no-commit window
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# DEC-5: per-day predicate semantics — a day carrying BOTH artifact types
# (changelog block + daily summary, per the 2026-08-06 AND fix) is "covered,"
# even if they belong to one machine (M1) and a DIFFERENT machine (M2) has
# genuinely older, unrecorded co-committed work on that same day. This is the
# INTENTIONAL semantic shift from the pre-de-machining behavior: retiring
# per-machine EXCLUSIVITY ATTRIBUTION wholesale (2026-07-19 PM ruling) means
# the prior TM5/TM6 "reconcile-only machine" false-negative guard rail, and
# the finer-grained per-machine coverage check it protected against, are BOTH
# gone — coverage is now day-level, full stop. See DEC-5
# (docs/plans/2026-07-19-de-machine-backfill-scan-per-day.md) and the
# 2026-07-01 multi-machine-coverage blind-spot-1 plan
# (docs/plans/2026-07-01-workday-complete-multi-machine-coverage.md) /
# 2026-06-30 striker incident it documents, whose per-machine coverage check
# this scanner deliberately no longer performs.
# ---------------------------------------------------------------------------


def test_dec5_any_block_covers_the_day_even_with_older_unrecorded_peer_work(repo, monkeypatch, capsys):
    day = "2026-03-20"
    root_sha = _commit_on(repo, "2026-03-18", "shared root", fname="root2.txt")

    # M2's genuinely older, unrecorded co-committed work on the same day —
    # under the OLD per-machine predicate this would have been flagged as a
    # gap (TM5/TM6 blind-spot-1 territory). Under the new per-day predicate
    # it is NOT independently checked.
    _branch_commit_from(repo, f"work/m2/{day}", root_sha, day, "08:00:00Z", "m2 older unrecorded work", "m2older.txt")

    # M1's own record for the day, with no per-machine keying required
    # anymore — its existence (both artifact types, per the 2026-08-06 AND
    # fix) covers the whole day.
    (repo / "archive" / "daily-summaries" / f"{day}.md").write_text("m1's summary\n")
    week_changelog_dir = repo / "state" / "week-changelog"
    week_changelog_dir.mkdir(parents=True)
    (week_changelog_dir / f"{day}.md").write_text("m1's block\n")

    rc = _run_scan(repo, monkeypatch, capsys, lookback=1, today="2026-03-21")
    out = capsys.readouterr().out

    assert rc == 0
    assert f"{day}\t" not in out  # day-level coverage suppresses the row despite m2's unrecorded work
