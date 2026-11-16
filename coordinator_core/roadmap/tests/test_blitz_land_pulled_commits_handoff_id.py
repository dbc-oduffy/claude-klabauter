"""land_wave names each pulled baton's record path so its stamps ride the landing commit.

DoE's land-wave collects commit candidates from `state/handoffs` path strings in the
landing reply; a bare `pulled` id list left a stamped `handoff_id` uncommitted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.roadmap import blitz_land as bl
from coordinator_core.roadmap import plan_gate as pg


@pytest.fixture(autouse=True)
def _isolated_gate(monkeypatch):
    monkeypatch.setattr(bl, "shipped_in_refusals", lambda root, shas: {})

    def _report(root: Path, roadmap_id=None, **_k) -> dict:
        _, records = pg.scan_batons(root, include_archived=False)
        batons = [
            {
                "id": r["id"], "stub_id": r["stub_id"], "path": r["path"], "title": r["title"],
                "sized": bool(r["sizing_objects"]), "plan": None,
                "sizing_objects": r["sizing_objects"],
                "execution_gate": {"open": False, "blocking": []},
            }
            for r in records
        ]
        return {
            "batons": batons, "waves": [],
            "counts": {"candidates": 0, "planning_open": 0, "execution_open": 0, "unschedulable": 0},
        }

    monkeypatch.setattr(bl.gate_v2, "compute_gate_report", _report)


def _stamped_baton(root: Path, stub_id: str) -> str:
    (root / ".git").mkdir(exist_ok=True)
    lines = [
        "kind: roadmap-baton", f"title: {stub_id}", f"stub_id: {stub_id}",
        "status: open", "deployment_state: ready_to_fire", "baton_role: work",
        f'handoff_id: "hnd-{stub_id}-abc123"',
    ]
    p = root / "state" / "handoffs" / f"{stub_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + "\n".join(lines) + "\n---\n\nbody\n", encoding="utf-8")
    return f"state/handoffs/{stub_id}.md"


def test_a_pulled_baton_with_a_stamped_handoff_id_reports_its_record_path(tmp_path):
    baton = _stamped_baton(tmp_path, "b-1")

    out = bl.land_wave(tmp_path, {"waveIndex": 0, "pulled": [{"batonId": "b-1", "reason": "duplicate"}]})

    assert out["pulled"] == ["b-1"]
    assert out["pulled_records"] == [baton]


def test_a_pulled_plan_carrying_baton_reports_its_record_path(tmp_path):
    baton = _stamped_baton(tmp_path, "b-2")
    plan = tmp_path / "docs" / "plans" / "the-plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("---\ntitle: p\nstatus: draft\ncapabilities: []\n---\n\nbody\n", encoding="utf-8")

    out = bl.land_wave(
        tmp_path, {"waveIndex": 0, "pulled": [{"batonId": "b-2", "planPath": "docs/plans/the-plan.md"}]}
    )

    assert out["pulled_records"] == [baton]


def test_a_refused_pull_reports_no_record_path(tmp_path):
    out = bl.land_wave(tmp_path, {"waveIndex": 0, "pulled": [{"batonId": "ghost"}]})

    assert out["pulled"] == []
    assert out["pulled_records"] == []
    assert out["refused"]
