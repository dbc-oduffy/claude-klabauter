"""workday-complete emits the backfill-cap point only when the pre-A0 scan finds it live."""

from __future__ import annotations

import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from coordinator_core import _claude_klabauter_root
from coordinator_core.ops.test_workday_complete_backfill_scan import _commit_on, _make_repo
from coordinator_core.ops.workday_complete_backfill_scan import DEFAULT_LOOKBACK, GapRow, GapScan
from coordinator_core.session import record_homes
from coordinator_core.workday_complete import brief as wc_brief

_GOALS = {"today": [], "stale": [], "unreadable_error": None}
_DIRTY = {"ambiguous": False, "evidence": "synthetic: clean"}
_ID = "jp_step3_5_backfill_cap"
_PHASE_B = "d_step3_5_backfill_phase_b"


def _rows(n):
    return [GapRow(f"2026-02-{i + 1:02d}", i + 2, "b" * 40, "t" * 40) for i in range(n)]


def _points(scan):
    return {p["id"]: p for p in wc_brief._build_judgment_points(_GOALS, _DIRTY, backfill_scan_result=scan)}


def _phase_b(live):
    directives = wc_brief._build_directives({}, _GOALS, _DIRTY, backfill_cap_live=live)
    return next(d for d in directives if d["id"] == _PHASE_B)


def test_twelve_rows_list_twelve_dated_commit_counts():
    point = _points(GapScan(rows=_rows(12)))[_ID]
    lines = point["evidence"].splitlines()[1:]
    assert lines == [f"2026-02-{i + 1:02d}  {i + 2} commits" for i in range(12)]
    assert "12 pre-A0 gap row(s)" in point["question"]
    assert "post-A0" not in point["question"] + point["evidence"]


def test_ten_rows_or_fewer_without_older_gaps_emit_no_point():
    assert _ID not in _points(GapScan(rows=_rows(3)))
    assert _ID not in _points(GapScan(rows=_rows(wc_brief.BACKFILL_CAP_ROWS)))
    assert _ID not in _points(GapScan())


def test_older_gaps_alone_emit_the_point_with_a_summary_line():
    point = _points(GapScan(rows=_rows(1), older_gaps=["2026-01-05", "2026-01-03"]))[_ID]
    assert "2 older uncovered day(s)" in point["question"]
    assert point["evidence"].splitlines()[-1] == (
        f"older gaps: 2 day(s), 2026-01-03..2026-01-05 (before the {DEFAULT_LOOKBACK}-day window)"
    )


def test_unprobed_scan_keeps_the_point_with_generic_evidence():
    point = _points(None)[_ID]
    assert "pre-A0" in point["evidence"]


def test_phase_b_waits_on_the_point_only_while_it_is_live():
    assert _phase_b(True)["depends_on"] == _ID
    assert _phase_b(False)["depends_on"] is None


def test_scan_directive_uses_the_one_lookback_constant():
    directives = wc_brief._build_directives({}, _GOALS, _DIRTY)
    scan_directive = next(d for d in directives if d["id"] == "d_step3_5_backfill_scan")
    assert scan_directive["args"][:2] == ["--lookback", str(wc_brief.LOOKBACK_DAYS)]


def test_scan_failure_degrades_to_no_point_and_a_narration_line(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("git unavailable")

    monkeypatch.setattr(wc_brief, "backfill_scan", _boom)
    scan, note = wc_brief._compute_backfill_scan()
    assert scan.rows == [] and scan.older_gaps == []
    assert "git unavailable" in note and _ID in note
    assert not wc_brief._backfill_cap_live(scan)


def test_brief_end_to_end_emits_the_point_from_the_scan(monkeypatch):
    monkeypatch.setattr(
        wc_brief,
        "resolve_operator_config",
        lambda env=None: {"settings_home": "", "claude_klabauter_bin": "", "claude_klabauter_root": "", "content_root": ""},
    )
    monkeypatch.setattr(wc_brief, "_compute_open_day_goals", lambda: _GOALS)
    monkeypatch.setattr(wc_brief, "_compute_dirty_tree_verdict", lambda: _DIRTY)
    monkeypatch.setattr(wc_brief, "head_branch", lambda _p: "work/x")
    monkeypatch.setattr(wc_brief, "_compute_register_stall", lambda: wc_brief.StallReport())
    monkeypatch.setattr(wc_brief, "_compute_backfill_scan", lambda: (GapScan(rows=_rows(11)), ""))
    code, envelope = wc_brief.brief(decisions={})
    assert code == 0
    assert _ID in {p["id"] for p in envelope["judgment_points"]}
    phase_b = next(d for d in envelope["directives"] if d["id"] == _PHASE_B)
    assert phase_b["depends_on"] == _ID

    monkeypatch.setattr(wc_brief, "_compute_backfill_scan", lambda: (GapScan(), "scan note"))
    code, envelope = wc_brief.brief(decisions={})
    assert _ID not in {p["id"] for p in envelope["judgment_points"]}
    assert "scan note" in envelope["narration"]
    phase_b = next(d for d in envelope["directives"] if d["id"] == _PHASE_B)
    assert phase_b["depends_on"] is None


TODAY = "2026-03-15"

# Process spawns of the brief's one in-process scan over an all-covered fixture window.
SCAN_GIT_SPAWNS = 3
CLAUDE_HOME_EXTRA_SPAWNS = 1  # `_machine_local_get` interpreter start, only when the root is the claude home


def _covered_repo(tmp_path_factory) -> Path:
    repo = _make_repo(tmp_path_factory)
    changelog_dir = Path(record_homes.home_dir(str(repo), "week-changelog"))
    changelog_dir.mkdir(parents=True)
    for i in range(1, DEFAULT_LOOKBACK + 1):
        day = (date.fromisoformat(TODAY) - timedelta(days=i)).isoformat()
        sha = _commit_on(repo, day, f"work {day}")
        (repo / "archive" / "daily-summaries" / f"{day}.md").write_text(f"summary\ncovered_tip_sha: {sha}\n")
        (changelog_dir / f"{day}.md").write_text("changelog\n")
    return repo


def _scan_spawns(repo: Path, monkeypatch) -> dict[str, int]:
    monkeypatch.setenv("COORDINATOR_ROOT", str(repo))
    monkeypatch.setenv("COORDINATOR_ROOT_WARN_SUPPRESS", "1")
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    _claude_klabauter_root.clear_machine_local_cache()
    counts = {"git": 0, "python": 0, "other": 0}
    orig = subprocess.run

    def _counting_run(cmd, *a, **kw):
        head = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else ""
        counts["git" if head == "git" else "python" if head == sys.executable else "other"] += 1
        return orig(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", _counting_run)
    try:
        result = wc_brief.backfill_scan(str(repo), DEFAULT_LOOKBACK, TODAY)
    finally:
        monkeypatch.setattr(subprocess, "run", orig)
        _claude_klabauter_root.clear_machine_local_cache()
    assert result.rows == [] and result.older_gaps == [], "all-covered fixture must report no gap"
    return counts


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_brief_scan_spawn_count_is_pinned(tmp_path_factory, monkeypatch):
    counts = _scan_spawns(_covered_repo(tmp_path_factory), monkeypatch)
    assert counts == {"git": SCAN_GIT_SPAWNS, "python": 0, "other": 0}


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_brief_scan_in_the_claude_home_adds_the_machine_local_interpreter_spawn(
    tmp_path_factory, tmp_path, monkeypatch
):
    repo = _covered_repo(tmp_path_factory)
    stub = tmp_path / "machine_local_stub.py"
    stub.write_text(f"print({str(repo)!r})\n")
    monkeypatch.setenv("CLAUDE_HOME", str(repo))
    monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(stub))
    counts = _scan_spawns(repo, monkeypatch)
    assert counts == {"git": SCAN_GIT_SPAWNS, "python": CLAUDE_HOME_EXTRA_SPAWNS, "other": 0}
