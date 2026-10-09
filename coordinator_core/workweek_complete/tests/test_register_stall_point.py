"""The workweek-complete brief names requirement-register stalls and omits the point when none."""
from __future__ import annotations

import pytest

from coordinator_core.ops.requirement_register import StallReport
from coordinator_core.workweek_complete import brief as brief_mod

_ID = "jp_requirement_register_stall"


@pytest.fixture
def operator_env(tmp_path):
    settings_home = tmp_path / "settings-home"
    (settings_home / "machine-local").mkdir(parents=True)
    root = tmp_path / "root"
    root.mkdir()
    (settings_home / "machine-local" / ".coordinator-content-root").write_text(str(root))
    return {"COORDINATOR_SETTINGS_HOME": str(settings_home), "HOME": str(tmp_path)}


_FULL = StallReport(
    stale_rows=["state/sizings/s.yaml row r1 (partial, claimed by p1, no progress since 2026-09-01)"],
    stuck_plans=["docs/plans/p.md (executing, untouched 20d, sizing state/sizings/s.yaml)"],
    unclaimed_rows=["state/sizings/s.yaml row r2 (ui)"],
)


def test_point_names_stale_stuck_and_unclaimed():
    points = brief_mod._build_judgment_points(register_stall=_FULL)
    point = next(p for p in points if p["id"] == _ID)
    for needle in ("row r1", "docs/plans/p.md", "row r2"):
        assert needle in point["evidence"]
    assert point["recommendation"] is not None


def test_point_absent_when_report_empty_or_omitted():
    assert _ID not in {p["id"] for p in brief_mod._build_judgment_points()}
    assert _ID not in {
        p["id"] for p in brief_mod._build_judgment_points(register_stall=StallReport())
    }


def test_brief_reports_stall_in_narration_not_asked(operator_env, monkeypatch):
    monkeypatch.setattr(brief_mod, "_compute_register_stall", lambda: _FULL)
    exit_code, envelope = brief_mod.brief(env=operator_env)
    assert exit_code == 0, envelope
    assert _ID not in {p["id"] for p in envelope["judgment_points"]}
    assert _ID in envelope["narration"]


def test_brief_omits_stall_when_report_empty(operator_env, monkeypatch):
    monkeypatch.setattr(brief_mod, "_compute_register_stall", lambda: StallReport())
    exit_code, envelope = brief_mod.brief(env=operator_env)
    assert exit_code == 0, envelope
    assert _ID not in envelope["narration"]
    assert _ID not in {p["id"] for p in envelope["judgment_points"]}
