"""Workday-start catch-up entry: the gap detector and the single `workday-complete --catch-up` agent entry."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from coordinator_core.git.run import GitResult
from coordinator_core.workday_start import contract, manifest, run_spec

TODAY = dt.date(2026, 10, 20)


@pytest.fixture(autouse=True)
def _pin_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("coordinator_core.daily_day.local_day", lambda repo_root=None: TODAY.isoformat())


def _conclude(root: Path, day: str, *, stub: bool = False) -> None:
    for rel in (f"state/week-changelog/{day}.md", f"archive/daily-summaries/{day}.md"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("(synthesized backfill)" if stub and "changelog" in rel else "x", encoding="utf-8")


def _commit_ts(day: str) -> int:
    return int(dt.datetime.fromisoformat(f"{day}T12:00:00").timestamp())


class _Git:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, days: list[str], rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        out = "\n".join(str(_commit_ts(d)) for d in days)

        def fake(args, **kw):
            self.calls.append(list(args))
            return GitResult(returncode=rc, stdout=out, stderr="", timed_out=False)

        monkeypatch.setattr(manifest, "run_git", fake)


def _all_but(days: list[str]) -> list[str]:
    window = [(TODAY - dt.timedelta(days=n)).isoformat() for n in range(14, 0, -1)]
    return [d for d in window if d not in days]


def test_fully_concluded_window_spawns_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git = _Git(monkeypatch, [])
    for d in _all_but([]):
        _conclude(tmp_path, d)
    assert manifest.catchup_gap_detect(tmp_path) == (0, "")
    assert git.calls == []


def test_gap_day_with_commits_is_named_after_one_git_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gap = "2026-10-15"
    git = _Git(monkeypatch, [gap])
    for d in _all_but([gap]):
        _conclude(tmp_path, d)
    code, text = manifest.catchup_gap_detect(tmp_path)
    assert code == 0 and gap in text and "1 past day(s)" in text
    assert len(git.calls) == 1 and "--no-merges" in git.calls[0]


def test_gap_day_without_commits_is_not_a_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _Git(monkeypatch, [])
    for d in _all_but(["2026-10-15"]):
        _conclude(tmp_path, d)
    assert manifest.catchup_gap_detect(tmp_path) == (0, "")


def test_today_is_never_a_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _Git(monkeypatch, [TODAY.isoformat()])
    for d in _all_but([]):
        _conclude(tmp_path, d)
    assert manifest.catchup_gap_detect(tmp_path) == (0, "")


def test_synthesized_stub_does_not_conclude_a_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _Git(monkeypatch, ["2026-10-15"])
    for d in _all_but(["2026-10-15"]):
        _conclude(tmp_path, d)
    _conclude(tmp_path, "2026-10-15", stub=True)
    assert "2026-10-15" in manifest.catchup_gap_detect(tmp_path)[1]


def test_changelog_without_summary_is_a_gap_and_archived_block_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _Git(monkeypatch, ["2026-10-14", "2026-10-15"])
    for d in _all_but(["2026-10-14", "2026-10-15"]):
        _conclude(tmp_path, d)
    (tmp_path / "state" / "week-changelog").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "week-changelog" / "2026-10-14.md").write_text("x", encoding="utf-8")
    archived = tmp_path / "archive" / "week-changelogs" / "2026-10-12"
    archived.mkdir(parents=True)
    (archived / "2026-10-15.md").write_text("x", encoding="utf-8")
    (tmp_path / "archive" / "daily-summaries" / "2026-10-15.md").write_text("x", encoding="utf-8")
    text = manifest.catchup_gap_detect(tmp_path)[1]
    assert "2026-10-14" in text and "2026-10-15" not in text


def test_git_failure_is_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _Git(monkeypatch, ["2026-10-15"], rc=128)
    assert manifest.catchup_gap_detect(tmp_path) == (0, "")


def test_step_is_a_daily_inproc_in_its_own_stage_with_a_measured_row() -> None:
    record = next(r for r in manifest.STEP_TABLE if r["id"] == manifest.CATCH_UP_STEP)
    assert "inproc" in record and not manifest.CATCH_UP_STEP.startswith("weekly")
    run = manifest.RUN_FIELDS[manifest.CATCH_UP_STEP]
    assert run["stage"] == "catch-up" and "gate" not in run
    assert run["stage"] in contract.LAUNCH_STAGES
    row = json.loads(manifest.STEP_COSTS_PATH.read_text(encoding="utf-8"))["steps"][manifest.CATCH_UP_STEP]
    assert row["basis"] == "measured" and row["process_ms_median"] < 200


def _row(status: str, stdout: str) -> dict:
    return {"id": manifest.CATCH_UP_STEP, "status": status, "stdout": stdout, "ledger_path": "x"}


def _backfill(groups: list[dict]) -> list[dict]:
    return [g for g in groups if g["id"] == "backfill"]


def test_gap_emits_exactly_one_catch_up_entry() -> None:
    line = "- Catch-up: 2 past day(s) with commits and no concluded wrap: 2026-10-14, 2026-10-15"
    groups = run_spec.agent_groups({}, [_row("ran", line)], None)
    (group,) = _backfill(groups)
    (entry,) = group["entries"]
    assert entry["argv"] == ["workday-complete", "--catch-up"]
    assert entry["inputs"] == {"days": ["2026-10-14", "2026-10-15"]}


@pytest.mark.parametrize("rows", [[], [_row("ran", "")], [_row("failed", "2026-10-14")]])
def test_no_gap_emits_no_entry(rows: list[dict]) -> None:
    assert _backfill(run_spec.agent_groups({}, rows, None)) == []
