"""Tests for the weekly picker: ranking, bounded previews, unmatched count, token report, CLI."""

from __future__ import annotations

import datetime
import subprocess
from pathlib import Path

from coordinator_core.ops import weekly_picker
import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

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


def test_git_fallback_builds_packet_with_deleted_plan(tmp_path: Path) -> None:
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
