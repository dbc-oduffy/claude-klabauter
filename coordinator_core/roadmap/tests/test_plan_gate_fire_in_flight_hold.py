"""A minted baton held for a fire in flight is withheld and reported by plan_gate, never re-planned.

Fixtures are literal text shaped like a minted spinoff baton; no minting code is exercised.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.roadmap import plan_gate as pg

HOLD_REASON = "plan-blitz fire in flight"
HOLD_CITE = "state/blitz/fire-receipt.json"

_BATON = """---
kind: spinoff
title: minted
stub_id: minted-1
status: open
deployment_state: ready_to_fire
baton_role: work
deliverable_id: sizing-mints-baton
sizing_object: state/sizing/minted.json
{extra}---

body
"""


def _mint(root: Path, extra: str = "") -> None:
    path = root / "state" / "handoffs" / "minted-1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_BATON.format(extra=extra), encoding="utf-8")


def _hold() -> str:
    return f'plan_blitz_hold_reason: "{HOLD_REASON}"\nplan_blitz_hold_cite: {HOLD_CITE}\n'


def _entry(report: dict) -> dict:
    return next(b for b in report["batons"] if b["id"] == "minted-1")


def test_fire_held_minted_baton_is_reported_held_and_not_a_candidate(tmp_path):
    _mint(tmp_path, _hold())
    report = pg.assemble_plan_gate(tmp_path)

    rows = [h for h in report["held"] if h["baton"] == "minted-1"]
    assert len(rows) == 1
    assert rows[0]["reason"] == HOLD_REASON
    assert rows[0]["cite"] == HOLD_CITE
    assert not [b for b in report["batons"] if b["id"] == "minted-1"]
    assert "minted-1" not in str(report["waves"])


def test_released_minted_baton_with_draft_plan_is_linked_not_unplanned(tmp_path):
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("---\ntitle: p\nstatus: draft\n---\n\nbody\n", encoding="utf-8")
    _mint(tmp_path, "governing_plan: docs/plans/p.md\n")
    report = pg.assemble_plan_gate(tmp_path, subject="minted-1")

    assert not [h for h in report["held"] if h["baton"] == "minted-1"]
    entry = _entry(report)
    assert entry["held"] is False
    assert entry["plan"]["path"] == "docs/plans/p.md"
