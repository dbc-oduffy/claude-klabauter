"""dispatch.ask_stage reports a declared targeted_test_python that does not resolve on this host, and stages unchanged."""

from __future__ import annotations

import sys

import pytest

from coordinator_core.ops.dispatch_emit import ask_stage, interpreter_pin
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_s_resume_stages import _committed_s_plan

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

UNSET_KEY = "c2-preflight-unset-key"


@pytest.fixture
def s_repo(tmp_path, monkeypatch):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.delenv(f"MACHINE_LOCAL_{UNSET_KEY.upper().replace('-', '_')}", raising=False)
    _committed_s_plan(tmp_path, [("A1", "open")])
    return tmp_path


def _declare(root, value: str) -> None:
    (root / "coordinator.local.md").write_text(f"---\ntargeted_test_python: {value}\n---\n", encoding="utf-8")


def _stage_reply(root, run_id: str) -> dict:
    return ask_stage._handler({"run_id": run_id, "plan_path": PLAN_REL, "writes": ["a.py"]}, repo_root=root)


def test_unresolved_machine_local_key_is_reported_with_its_remedy_and_the_stage_proceeds(s_repo):
    _declare(s_repo, f"machine-local:{UNSET_KEY}")

    reply = _stage_reply(s_repo, "r-unset")

    assert "manifest_handle" in reply and "error" not in reply, reply
    assert UNSET_KEY in reply["interpreter_pin_defect"]
    assert interpreter_pin.REMEDY in reply["interpreter_pin_defect"]
    assert any(UNSET_KEY in w and interpreter_pin.DEFECT_PREFIX in w for w in reply["warnings"]), reply


def test_stage_at_default_arguments_fills_the_report_before_any_row_is_staged(s_repo):
    _declare(s_repo, f"machine-local:{UNSET_KEY}")
    report: dict = {}

    ask_stage.stage(s_repo, run_id="r-direct", plan_rel=PLAN_REL, writes=["a.py"], family_report=report)

    assert UNSET_KEY in report["interpreter_pin_defect"]
    assert interpreter_pin.REMEDY in report["interpreter_pin_defect"]


def test_a_resolvable_pin_stages_unchanged(s_repo):
    _declare(s_repo, sys.executable)

    reply = _stage_reply(s_repo, "r-ok")

    assert "manifest_handle" in reply
    assert "interpreter_pin_defect" not in reply and "warnings" not in reply, reply


def test_no_pin_stages_unchanged(s_repo):
    reply = _stage_reply(s_repo, "r-none")

    assert "manifest_handle" in reply
    assert "interpreter_pin_defect" not in reply, reply


def test_the_preflight_spawns_no_process(s_repo, monkeypatch):
    import subprocess

    _declare(s_repo, f"machine-local:{UNSET_KEY}")

    def _no_spawn(*a, **k):
        raise AssertionError("preflight spawned a process")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    report: dict = {}

    ask_stage._report_interpreter_pin_defect(s_repo, report)

    assert UNSET_KEY in report["interpreter_pin_defect"]
