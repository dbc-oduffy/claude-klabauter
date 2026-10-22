"""A manifest the resume gate staged is vouched for: dispatch.ask_stage on the same run replies a handle."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_stage, ask_stage_receipt
from coordinator_core.ops.dispatch_emit.ask_gate import _handler
from coordinator_core.ops.dispatch_emit.ask_stage import record_dir
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_s_resume_stages import _committed_s_plan
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import (  # noqa: F401
    PLAN,
    SIZING,
    _met_out_of_band,
    halted,
    repo,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


@pytest.fixture
def s_repo(tmp_path, monkeypatch):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    return tmp_path


def _assert_ask_stage_accepts(root, run_id, plan_rel):
    reply = ask_stage._handler({"run_id": run_id, "plan_path": plan_rel, "writes": ["a.py"]}, repo_root=root)
    assert "manifest_handle" in reply and "error" not in reply, reply


def test_s_resume_gate_stage_then_ask_stage_is_not_refused(s_repo):
    _committed_s_plan(s_repo, [("A1", "coded"), ("A2", "open")])
    out = _handler({"sizing_path": REL, "run_id": "r1", "writes": ["a.py"]}, repo_root=s_repo)
    assert out["resume_plan"] == PLAN_REL and "manifest_handle" in out, out

    _assert_ask_stage_accepts(s_repo, "r1", PLAN_REL)


def test_ledger_resume_gate_stage_then_ask_stage_is_not_refused(halted):  # noqa: F811
    _met_out_of_band(halted)
    out = _handler({"sizing_path": SIZING, "run_id": "newrun", "writes": ["a.py"]}, repo_root=halted)
    assert out["resume_ledger"]["plan"] == PLAN and "manifest_handle" in out, out

    _assert_ask_stage_accepts(halted, "newrun", PLAN)


def test_a_manifest_no_gate_wrote_is_still_refused(s_repo):
    _committed_s_plan(s_repo, [("A1", "coded"), ("A2", "open")])
    _handler({"sizing_path": REL, "run_id": "r1", "writes": ["a.py"]}, repo_root=s_repo)
    (record_dir(s_repo, "r1") / ask_stage_receipt.RECEIPT_NAME).unlink()

    reply = ask_stage._handler({"run_id": "r1", "plan_path": PLAN_REL, "writes": ["a.py"]}, repo_root=s_repo)

    assert reply["error"].startswith(ask_stage.STAGE_RECEIPT_MISSING), reply
