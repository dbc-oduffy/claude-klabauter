"""An orphaned run is named by the gate, adopted through the op, and resumed without a re-plan."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import halt_refire as hr
from coordinator_core.ops.dispatch_emit import orphan_adopt as oa
from coordinator_core.ops.dispatch_emit import ask_stage, ask_stage_receipt
from coordinator_core.ops.dispatch_emit.ask_gate import _handler as gate_handler
from coordinator_core.ops.dispatch_emit.ask_stage import record_dir
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_s_resume_stages import _committed_s_plan

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

ORPHAN = "warp-2026-10-01-gate-20261028T100000-aaaaaa"
FRESH = "warp-2026-10-01-gate-20261028T120000-cccccc"
DEAD = "99999999-0000-0000-0000-000000000000"
ME = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def orphan(repo, monkeypatch):  # noqa: F811
    """A run the dead session `DEAD` staged (plan, manifest, receipt) and never finished."""
    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda sid, cwd=None: False)
    _committed_s_plan(repo, [("A1", "coded"), ("A2", "open")])
    staged = ask_stage._handler({"run_id": ORPHAN, "plan_path": PLAN_REL, "writes": ["a.py"]}, repo_root=repo)
    assert "manifest_handle" in staged, staged
    d = record_dir(repo, ORPHAN)
    (d / "run.json").write_text(json.dumps({"sizing_rel": REL}), encoding="utf-8")
    (d / "run-stage.json").write_text(
        json.dumps({"session_id": DEAD, "stage": "staged", "started_at": "t0", "steps_planned": 1}), encoding="utf-8"
    )
    return repo


def test_gate_adopts_an_orphan_in_run_and_resumes_without_halting(orphan):
    out = gate_handler({"sizing_path": REL, "run_id": FRESH, "writes": ["a.py"]}, repo_root=orphan)

    assert out["halt"] is None and out["resume_plan"] == PLAN_REL and out["resume_rows"] == ["A2"], out


def _adopt_fails(monkeypatch):
    monkeypatch.setattr(oa, "_handler", lambda params, repo_root=None: {"ok": False, "reason": "adopt refused"})


def test_gate_refuses_an_orphan_naming_the_adopt_call(orphan, monkeypatch):
    _adopt_fails(monkeypatch)
    out = gate_handler({"sizing_path": REL, "run_id": FRESH, "writes": ["a.py"]}, repo_root=orphan)

    assert out["arm"] is None and out["halt"]["kind"] == oa.ORPHAN_PENDING, out
    assert out["halt"]["orphan_run"] == ORPHAN
    nxt = out["halt"]["next_action"]
    assert hr.is_exit_next_action(nxt) and nxt["route"] == hr.ROUTE_ADOPT
    assert ORPHAN in nxt["call"] and oa.OP_NAME in nxt["call"] and "Workflow" not in nxt["call"]


def test_adopted_orphan_resumes_without_a_replan_and_ask_stage_is_already_staged(orphan):
    reply = oa._handler({"run_id": ORPHAN, "session_id": ME}, repo_root=orphan)
    assert reply["ok"] is True and reply["resume_stage"] == "execute", reply

    out = gate_handler({"sizing_path": REL, "run_id": FRESH, "writes": ["a.py"]}, repo_root=orphan)
    assert out["halt"] is None and out["resume_plan"] == PLAN_REL and out["resume_rows"] == ["A2"], out
    assert "spine_plan" not in out

    for _ in range(2):
        again = ask_stage._handler(
            {"run_id": ORPHAN, "plan_path": PLAN_REL, "writes": ["a.py"]}, repo_root=orphan
        )
        assert again.get(ask_stage_receipt.ALREADY_STAGED_KEY) is True, again


def test_orphan_pending_fire_does_not_make_the_orphan_superseded(orphan, monkeypatch):
    _adopt_fails(monkeypatch)
    out = gate_handler({"sizing_path": REL, "run_id": FRESH, "writes": ["a.py"]}, repo_root=orphan)
    assert out["halt"]["kind"] == oa.ORPHAN_PENDING
    fresh = record_dir(orphan, FRESH)
    fresh.mkdir(parents=True, exist_ok=True)
    marker = fresh / "halted.json"
    marker.write_text(json.dumps({"status": "halted", "halted": oa.ORPHAN_PENDING}), encoding="utf-8")

    verdict = oa.orphan_verdict(orphan, ORPHAN, ME)

    assert verdict["orphaned"] is True and verdict["superseded_by"] is None, verdict


def test_a_live_owner_is_not_halted_on(orphan, monkeypatch):
    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda sid, cwd=None: sid == DEAD)
    out = gate_handler({"sizing_path": REL, "run_id": FRESH, "writes": ["a.py"]}, repo_root=orphan)
    assert (out["halt"] or {}).get("kind") != oa.ORPHAN_PENDING, out
