"""warp roadmap arm: a roadmap sizing is accepted and composes the four stages in order."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.ask_roadmap import approver_for
from coordinator_core.ops.dispatch_emit.sizing_fire import (
    ARM_M_PLUS,
    ARM_ROADMAP,
    effective_route,
    resolve_arm,
)
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

REL = "state/sizings/2026-10-02-rm.yaml"
ACCEPTED = {"pm_quote": "yes", "on": "2026-10-02", "mode": "pm"}
_PB = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_RB = "export const meta = { name: 'rb', phases: [{ title: 'Synthesize', detail: 'x' }] };\nreturn { gate_report_path: 'g.json' };"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    return tmp_path


def _put(repo, route="roadmap", xl_exit=None, mode="pm", tshirt="XL"):
    doc = {
        "schema": "sizing-object", "name": "rm", "intent": "an XL job",
        "estimate": {"tshirt": tshirt, "provisional": True}, "route": route,
        "detents": [], "fork": None, "xl_exit": xl_exit, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-fixture-abc123", "interaction_mode": mode,
        "exit_criterion": {"statement": "done", "accepted": ACCEPTED},
    }
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    return doc


def _compose(repo, **over):
    kw = dict(
        repo_root=repo.as_posix(), prompt=None, sizing_rel=REL, run_id="r1", session_id=None,
        wrap_stage=lambda t: (_PB, ["Plan"]), plan_blitz_text="stub", roadmap_blitz_text=_RB,
        plan_blitz_args={"x": 1}, **REVIEW_KW,
    )
    kw.update(over)
    return ask_compose.compose_ask_script(**kw)


def test_roadmap_route_is_not_refused_and_mints_no_baton(repo):
    _put(repo)
    verdict = gate(repo, REL)
    assert verdict.halt is None and verdict.arm == ARM_ROADMAP and verdict.baton is None


def test_pm_decision_with_roadmap_exit_resolves_to_roadmap(repo):
    _put(repo, route="pm-decision", xl_exit="roadmap")
    assert gate(repo, REL).arm == ARM_ROADMAP


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_pm_decision_with_accept_multi_session_resolves_to_plan(repo):
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, **no_console_creationflags())
    doc = _put(repo, route="pm-decision", xl_exit="accept_multi_session")
    assert effective_route(doc) == "plan" and resolve_arm(doc) == ARM_M_PLUS
    verdict = gate(repo, REL)
    assert verdict.halt is None and verdict.arm == ARM_M_PLUS and verdict.baton


def test_unresolved_pm_decision_still_refuses_as_room(repo):
    _put(repo, route="pm-decision", xl_exit=None)
    halt = gate(repo, REL).halt
    assert halt["kind"] == "room" and halt["route"] == "pm-decision"


def test_shape_route_still_halts_as_room(repo):
    _put(repo, route="shape")
    assert gate(repo, REL).halt["kind"] == "room"


def test_composed_script_chains_four_stages_in_order(repo):
    _put(repo)
    script = _compose(repo)
    marks = ["roadmapBlitz({", "planBlitz({", "phase('execute')", "phase('workstream-complete')"]
    pos = [script.index(m, script.index("phase('gate')")) for m in marks]
    assert pos == sorted(pos)
    assert "gateReportPath: _rb.gate_report_path" in script and "batons: w.batons" in script
    assert "if (!_halted) {\n  phase('workstream-complete')" in script
    assert not any(f.severity is Severity.ERROR for f in run_checks(script))


@pytest.mark.parametrize(
    "mode,approver", [("hands-on", "pm"), ("pm", "apm"), ("ceo", "apm")]
)
def test_approver_follows_interaction_mode(repo, mode, approver):
    _put(repo, mode=mode)
    assert approver_for(mode) == approver
    assert f"approver: '{approver}'" in _compose(repo)


def test_non_roadmap_script_is_unchanged_by_the_roadmap_arm(repo, monkeypatch):
    _put(repo, route="plan")
    with_arm = _compose(repo)
    monkeypatch.setattr(ask_compose, "ARM_ROADMAP", "never")
    assert with_arm == _compose(repo)
    assert "roadmapBlitz" not in with_arm
