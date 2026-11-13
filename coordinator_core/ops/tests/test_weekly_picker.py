"""Tests for the weekly picker: ranking, bounded previews, unmatched count, token report, CLI."""

from __future__ import annotations

import datetime
import json
import sqlite3
import subprocess
from pathlib import Path

from coordinator_core.ops import weekly_picker

MONDAY = datetime.date(2026, 11, 9)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True,
    )


def _daily(repo: Path, day: str, lines: list) -> None:
    d = repo / "state" / "week-changelog"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day}.md").write_text("## " + day + "\n\n" + "\n".join(lines) + "\n", encoding="utf-8")


def _make_db(path: Path, rows: list, previews: dict) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE plans_completed (plan, sha, summary, sizing, ranked, completed_at)")
    conn.execute("CREATE TABLE plan_preview (plan, sha, preview)")
    conn.executemany("INSERT INTO plans_completed VALUES (?,?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO plan_preview VALUES (?,?,?)", [(p, s, t) for (p, s), t in previews.items()])
    conn.commit()
    conn.close()


def _view_fixture(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _daily(repo, "2026-11-09", ["- [S] `docs/plans/small.md` did a small thing"])
    _daily(repo, "2026-11-10", [
        "- [XL] `docs/plans/big.md` did a big thing",
        "- [?] `docs/plans/nosize.md` no sizing known",
    ])
    _daily(repo, "2026-11-11", ["- [XS] `docs/plans/tiny.md` tiny"])
    db = tmp_path / "doc_files.sqlite"
    _make_db(
        db,
        [
            ("docs/plans/small.md", "a1", "s", "S", 1, "2026-11-09"),
            ("docs/plans/big.md", "b2", "s", "XL", 1, "2026-11-10"),
            ("docs/plans/tiny.md", "c3", "s", "XS", 1, "2026-11-11"),
            ("docs/plans/unnamed.md", "d4", "s", "L", 1, "2026-11-11"),
        ],
        {("docs/plans/big.md", "b2"): "x" * 25000, ("docs/plans/small.md", "a1"): "small body"},
    )
    return repo, db


def test_ranks_by_sizing_then_date_with_unsized_after_xs(tmp_path):
    repo, db = _view_fixture(tmp_path)
    packet = weekly_picker.build_packet(repo, MONDAY, index_path=db)
    assert [c["plan"] for c in packet["candidates"]] == [
        "docs/plans/big.md", "docs/plans/small.md", "docs/plans/tiny.md", "docs/plans/nosize.md",
    ]
    first = packet["candidates"][0]
    assert first["sha"] == "b2" and "big thing" in first["daily_line"]


def test_preview_capped_and_unmatched_counted(tmp_path):
    repo, db = _view_fixture(tmp_path)
    packet = weekly_picker.build_packet(repo, MONDAY, index_path=db)
    assert all(len(c["preview"]) <= 10000 for c in packet["candidates"])
    assert len(packet["candidates"][0]["preview"]) == 10000
    assert packet["unmatched"] == 1  # unnamed.md completed but in no daily line


def test_tokens_and_chars_reported_and_nudge_present(tmp_path):
    repo, db = _view_fixture(tmp_path)
    packet = weekly_picker.build_packet(repo, MONDAY, index_path=db)
    assert packet["tokens"] > 0 and packet["chars"] > 10000
    assert packet["tokens"] == weekly_picker.estimate_tokens(
        json.dumps({k: v for k, v in packet.items() if k not in ("tokens", "chars")}, ensure_ascii=False)
    )
    assert "skim" in packet["nudge"].lower()


def test_days_outside_the_week_are_not_read(tmp_path):
    repo, db = _view_fixture(tmp_path)
    _daily(repo, "2026-11-16", ["- [XL] `docs/plans/nextweek.md` out of window"])
    packet = weekly_picker.build_packet(repo, MONDAY, index_path=db)
    assert "docs/plans/nextweek.md" not in [c["plan"] for c in packet["candidates"]]


def test_git_fallback_builds_packet_with_deleted_plan(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs" / "plans").mkdir(parents=True)
    _git(repo.parent, "init", str(repo))  # git -C <parent> init <repo>
    plan = repo / "docs" / "plans" / "gone.md"
    plan.write_text("# Gone\n" + "y" * 20000, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add plan")
    plan.unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "complete gone")
    today = datetime.date.today()
    week = today - datetime.timedelta(days=today.weekday())
    _daily(repo, week.isoformat(), ["- `docs/plans/gone.md` finished"])
    packet = weekly_picker.build_packet(repo, week, index_path=tmp_path / "absent.sqlite")
    assert [c["plan"] for c in packet["candidates"]] == ["docs/plans/gone.md"]
    cand = packet["candidates"][0]
    assert cand["sizing"] is None and len(cand["preview"]) <= 10000


def test_default_week_is_last_monday():
    assert weekly_picker.default_week(datetime.date(2026, 11, 13)) == datetime.date(2026, 11, 9)
    assert weekly_picker.default_week(datetime.date(2026, 11, 16)) == datetime.date(2026, 11, 9)


def test_cli_emits_json(tmp_path, capsys):
    repo, db = _view_fixture(tmp_path)
    rc = weekly_picker.main(["--week", "2026-11-09", "--repo", str(repo), "--index-path", str(db)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["week"] == "2026-11-09" and out["tokens"] > 0
    assert weekly_picker.main(["--week", "nope", "--repo", str(repo)]) == 2
