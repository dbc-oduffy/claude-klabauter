"""blitz_land refuses a first approval of a plan with no capabilities key, never a re-land."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.roadmap import blitz_land as bl
from coordinator_core.roadmap import plan_gate as pg

_SPINE = (
    "- id: C1\n  title: Ship the thing\n"
    "  body: Add the predicate and pin it with a failing-first test.\n"
    "  change_kind: code-edit\n  surface: coordinator_core/x.py\n"
    "  writes: [coordinator_core/x.py]\n  queue_scope: project\n  disposition: open\n"
)


def _plan(root: Path, slug: str, status: str, capabilities: str = "") -> str:
    (root / "coordinator_core").mkdir(parents=True, exist_ok=True)
    p = root / "docs" / "plans" / f"{slug}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\ntitle: {slug}\nstatus: {status}\n{capabilities}---\n\n"
        f"## Tasks\n\n```yaml plan-tasks\n{_SPINE}```\n",
        encoding="utf-8",
    )
    return f"docs/plans/{slug}.md"


def test_a_draft_keyless_plan_is_refused(tmp_path):
    plan = _plan(tmp_path, "keyless", "draft")
    refusal, _ = bl.prep_gate_refusal(tmp_path, plan)
    assert refusal and "CAPABILITIES capabilities-undeclared" in refusal
    assert "capabilities: []" in refusal


def test_a_draft_plan_declaring_empty_capabilities_is_not_refused(tmp_path):
    plan = _plan(tmp_path, "declared", "draft", "capabilities: []\n")
    refusal, _ = bl.prep_gate_refusal(tmp_path, plan)
    assert refusal is None


def test_an_already_approved_keyless_plan_is_not_refused_on_re_land(tmp_path):
    plan = _plan(tmp_path, "approved", "approved")
    refusal, _ = bl.prep_gate_refusal(tmp_path, plan)
    assert refusal is None


def test_land_wave_refuses_a_draft_keyless_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(bl, "shipped_in_refusals", lambda root, shas: {})
    (tmp_path / ".git").mkdir()
    baton = tmp_path / "state" / "handoffs" / "b-1.md"
    baton.parent.mkdir(parents=True)
    baton.write_text(
        "---\nkind: roadmap-baton\ntitle: b-1\nstub_id: b-1\nstatus: open\n"
        "deployment_state: ready_to_fire\nbaton_role: work\ndeliverable_id: dlv-b-1\n---\n\nbody\n",
        encoding="utf-8",
    )
    plan = _plan(tmp_path, "keyless", "draft")
    monkeypatch.setattr(
        bl.gate_v2, "compute_gate_report",
        lambda root, roadmap_id=None, **k: {"batons": [], "waves": [], "counts": {}},
    )

    out = bl.land_wave(tmp_path, {"waveIndex": 0, "ready": [{"batonId": "b-1", "planPath": plan}]})

    assert out["approved"] == []
    assert "CAPABILITIES capabilities-undeclared" in out["refused"][0]["reason"]
    assert pg._scan_fields(tmp_path / plan, frozenset({"status"})).get("status") == "draft"
