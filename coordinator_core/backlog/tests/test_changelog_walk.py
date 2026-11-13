"""Tests for the changelog walker: week span, flagging, dedupe, id resolution, no artifact opened."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.backlog.changelog_walk import iso_week_key, walk_changelogs
from coordinator_core.backlog.record_schema import WeekEntry


def _w(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _daily(root: Path, name: str, day: str, bullets: list[str]) -> None:
    body = "\n".join(f"- {b}" for b in bullets)
    _w(root, f"archive/daily-summaries/{name}.md",
       f"---\nrollup_schema: 1\ngrain: day\nperiod: {day}\n---\n\n# Daily\n\n## Work Completed\n\n{body}\n\n## Systems Affected\n\n- not a change\n")


def _week_summary(root: Path, start: str, end: str) -> None:
    _w(root, f"archive/week-changelogs/{start}/WEEK-SUMMARY.md",
       f"---\nrollup_schema: 1\ngrain: week\nweek_start: {start}\nweek_end: {end}\nnarrative: null\n---\n\n## Days\n")


def _by_week(entries: list[WeekEntry]) -> dict[str, WeekEntry]:
    return {e.week: e for e in entries}


def test_week_key_is_iso_monday():
    from datetime import date
    assert iso_week_key(date(2026, 5, 21)) == "2026-05-18"
    assert iso_week_key(date(2026, 5, 24)) == "2026-05-18"
    assert iso_week_key(date(2026, 5, 25)) == "2026-05-25"


def test_unflagged_change_plan_never_opened_and_flagged_one_cited_unopened(tmp_path, monkeypatch):
    plan = _w(tmp_path, "docs/plans/2026-05/unflagged-plan.md", "# plan\n")
    flagged = _w(tmp_path, "docs/plans/2026-05/flagged-plan.md", "# plan\n")
    _week_summary(tmp_path, "2026-05-25", "2026-05-31")
    _w(tmp_path, "archive/week-changelogs/2026-05-25/2026-05-27.md",
       "## 2026-05-27\n**Plans touched:** docs/plans/2026-05/unflagged-plan.md (status: removed)\n")
    _daily(tmp_path, "2026-05-27", "2026-05-27",
           ["**Landed thing**: see docs/plans/2026-05/flagged-plan.md (`abc1234`)"])

    opened: list[str] = []
    real = Path.read_text

    def spy(self, *a, **k):
        opened.append(self.as_posix())
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", spy)
    entries, ledger = walk_changelogs(tmp_path)

    week = _by_week(entries)["2026-05-25"]
    assert [c.cited_paths for c in week.flagged] == [("docs/plans/2026-05/flagged-plan.md",)]
    assert not any(p.endswith(("unflagged-plan.md", "flagged-plan.md")) for p in opened)
    assert plan.exists() and flagged.exists()
    assert "unflagged-plan.md" not in repr(entries)
    assert week.summary_path == "archive/week-changelogs/2026-05-25/WEEK-SUMMARY.md"
    assert ledger["weeks_without_summary"] == 0


def test_week_with_dailies_but_no_summary_counts_and_walks(tmp_path):
    _week_summary(tmp_path, "2026-05-25", "2026-05-31")
    _daily(tmp_path, "2026-05-27", "2026-05-27", ["**A**: one (`aaaaaaa`)"])
    _daily(tmp_path, "2026-06-10", "2026-06-10", ["**B**: two (`bbbbbbb`)"])
    _week_summary(tmp_path, "2026-06-15", "2026-06-21")
    entries, ledger = walk_changelogs(tmp_path)

    weeks = _by_week(entries)
    assert list(weeks) == ["2026-05-25", "2026-06-01", "2026-06-08", "2026-06-15"]
    assert ledger["weeks_total"] == 4 == ledger["weeks_walked"]
    assert weeks["2026-06-08"].summary_path is None
    assert [c.title for c in weeks["2026-06-08"].flagged] == ["B"]
    assert weeks["2026-06-01"].flagged == () and weeks["2026-06-01"].summary_path is None
    assert ledger["weeks_without_summary"] == 2
    assert ledger["weeks_without_source"] == 1


def test_weeks_total_spans_dailies_earlier_than_first_summary(tmp_path):
    _daily(tmp_path, "2026-04-02", "2026-04-02", ["**Early**: x (`1111111`)"])
    _week_summary(tmp_path, "2026-05-25", "2026-05-31")
    _, ledger = walk_changelogs(tmp_path)
    assert ledger["weeks_total"] == 9
    assert ledger["weeks_walked"] == ledger["weeks_total"]


def test_two_machine_summaries_sharing_a_commit_are_one_record(tmp_path):
    _daily(tmp_path, "2026-05-27-machine-a", "2026-05-27", ["**Fix**: shipped (`abc1234`)", "**Solo**: only here (`fff1111`)"])
    _daily(tmp_path, "2026-05-27-machine-b", "2026-05-27", ["**Fix**: shipped again (`abc1234`, `def5678`)"])
    entries, _ = walk_changelogs(tmp_path)
    changes = _by_week(entries)["2026-05-25"].flagged
    assert len(changes) == 2
    fix = next(c for c in changes if c.title == "Fix")
    assert fix.commits == ("abc1234", "def5678")
    assert fix.day == "2026-05-27"


def test_ids_resolve_by_listing_and_unresolved_is_recorded(tmp_path):
    _w(tmp_path, "docs/decisions/DR-344-the-brightline.md", "x")
    _w(tmp_path, "state/handoffs/2026-07/2026-07-01-thing-1b2c3d.md", "x")
    _w(tmp_path, "archive/completed/2026-05/entry-9f8e7d.md", "x")
    _daily(tmp_path, "2026-07-01", "2026-07-01", [
        "**Ids**: DR-344, handoff 1b2c3d, entry 9f8e7d, DR-999, handoff 000000 (`1234567`)",
    ])
    entries, ledger = walk_changelogs(tmp_path)
    change = _by_week(entries)["2026-06-29"].flagged[0]
    assert change.cited_paths == (
        "docs/decisions/DR-344-the-brightline.md",
        "state/handoffs/2026-07/2026-07-01-thing-1b2c3d.md",
        "archive/completed/2026-05/entry-9f8e7d.md",
    )
    assert {u["id"] for u in ledger["unresolved"]} == {"DR-999", "handoff 000000"}
    assert all(u["day"] == "2026-07-01" for u in ledger["unresolved"])


def test_only_work_completed_bullets_are_flagged(tmp_path):
    _daily(tmp_path, "2026-05-27", "2026-05-27", ["**Real**: yes (`abc1234`)"])
    entries, _ = walk_changelogs(tmp_path)
    titles = [c.title for e in entries for c in e.flagged]
    assert titles == ["Real"]


def test_empty_root_walks_nothing(tmp_path):
    entries, ledger = walk_changelogs(tmp_path)
    assert entries == [] and ledger["weeks_total"] == 0
