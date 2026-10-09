"""The requirement-register stall point rides the day and week cadences of the work family."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.orient_brief import _work

POINT_ID = "j-requirement-register-stall"


def _row(rid: str, status: str = "open", **kw) -> dict:
    row = {
        rr.ROW_ID: rid, rr.ROW_TEXT: f"text {rid}", rr.ROW_ANCHOR: "s1", rr.ROW_SURFACE: "ui",
        rr.ROW_STATUS: status, rr.ROW_CLAIMED_BY: [], rr.ROW_WIRED: False,
        rr.ROW_MET_BY: [], rr.ROW_LAST_PROGRESS_AT: None, rr.ROW_RULING: None,
    }
    row.update(kw)
    return row


def _tree(tmp_path: Path, *, with_register: bool = True) -> Path:
    git = tmp_path / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    sizings = tmp_path / "state" / "sizings"
    sizings.mkdir(parents=True)
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    if not with_register:
        (sizings / "s.yaml").write_text("schema: sizing-object\n", encoding="utf-8")
        return tmp_path
    rows = [
        _row("stale", "partial", claimed_by=["docs/plans/2026-01-01-stuck.md"],
             last_progress_at="2000-01-01"),
        _row("loose"),
    ]
    block = yaml.safe_dump({rr.REGISTER_KEY: {rr.ROWS_KEY: rows}}, sort_keys=False)
    (sizings / "s.yaml").write_text("schema: sizing-object\n" + block, encoding="utf-8")
    plan = plans / "2026-01-01-stuck.md"
    plan.write_text(
        "---\nplan_id: plan-a\nstatus: executing\ncreated: 2000-01-01\n"
        "sizing_object: state/sizings/s.yaml\n---\nbody\n",
        encoding="utf-8",
    )
    old = time.time() - 30 * 86400
    os.utime(plan, (old, old))
    return tmp_path


def _points(result) -> list[dict]:
    return [p for p in result.judgment_points if p["id"] == POINT_ID]


@pytest.mark.parametrize("cadence", ["day", "week"])
def test_point_names_all_three_stalls(tmp_path, cadence):
    points = _points(_work.collect(cadence, repo_root=_tree(tmp_path)))
    assert len(points) == 1
    evidence = points[0]["evidence"]
    assert "stale rows" in evidence and "row stale" in evidence
    assert "stuck plans" in evidence and "2026-01-01-stuck.md" in evidence
    assert "unclaimed rows" in evidence and "row loose" in evidence


def test_session_cadence_never_runs_the_scan(tmp_path, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(rr, "stall_report", lambda *a, **_k: calls.append(a))
    assert _points(_work.collect("session", repo_root=_tree(tmp_path))) == []
    assert calls == []


def test_scan_failure_is_guarded_at_day(tmp_path, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr(rr, "stall_report", boom)
    assert _points(_work.collect("day", repo_root=_tree(tmp_path))) == []
    assert "work/register-stall" in capsys.readouterr().err


@pytest.mark.parametrize("cadence", ["day", "week"])
def test_no_register_means_no_point(tmp_path, cadence):
    assert _points(_work.collect(cadence, repo_root=_tree(tmp_path, with_register=False))) == []
