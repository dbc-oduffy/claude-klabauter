"""compose_ask_script's plan-blitz no-ready branch: an unusable reviewer reply halts as a typed retryable halt, and every refusal names the refire."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import halt_refire as hr
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_REFUSAL, HALT_REVIEWER_UNREADY
from coordinator_core.ops.dispatch_emit.ask_roadmap import NO_READY_LITERAL
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW, shared_decls
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.usefixtures("m_plus_known_arm")

_NODE = shutil.which("node")
_SIZING = "docs/s.yaml"
_HEAD = "if (!_planRel) { _halted = "
_TAIL = "if (_halted && _halted.halted === 'split')"


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _script() -> str:
    return ask_compose.compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=_SIZING, run_id="run-1", session_id=None,
        wrap_stage=_stub_wrap, plan_blitz_text="stub", **REVIEW_KW,
    )


def _eval(blitz: dict, pre: str = "") -> dict:
    """Node-evaluate the composed no-ready branch over `blitz`, then the one-refire finaliser; return `_halted`."""
    if _NODE is None:
        pytest.skip("node is not installed")
    script = _script()
    start = script.index(_HEAD)
    branch = script[start : script.index(_TAIL, start)]
    consts = shared_decls(script)
    program = (
        f"const _runId = 'run-1'; const _sizingRel = {json.dumps(_SIZING)};\n{consts}\n"
        f"const _blitz = {json.dumps(blitz)}; let _planRel = null, _halted = null, _pullPlan = null, "
        "_pullRulings = [], _pullBlocking = 0, _pullFindings = null, _replanCapped = false, _pullSplit = false, "
        "_splitSlices = null, _pulledEntry = {};\n"
        + pre
        + ask_compose._refire_next_action_helper_js(_SIZING)
        + "\n{{" + branch + "\n_refireNextAction();\nconsole.log(JSON.stringify(_halted));\n"
    )
    proc = subprocess.run(
        [_NODE, "-"], input=program, capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _assert_refire(halted: dict) -> None:
    action = halted["next_action"]
    assert hr.is_refire_next_action(action), action
    assert _SIZING in action["call"] and "engine-ask" in action["call"], action


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize("kind", ["missing-tldr", "null-return", "errored"])
def test_a_reviewer_incident_halts_as_reviewer_unready_with_the_refire(kind):
    halted = _eval({"agentIncidents": [{"role": "reviewer", "kind": kind}], "pulled": []})
    assert halted["halted"] == HALT_REVIEWER_UNREADY and halted["retryable"] is True
    assert halted["reason"] == f"{NO_READY_LITERAL} (agent reviewer {kind})"
    _assert_refire(halted)
    assert "workflow.revise {run_id: run-1" in halted["next_action"]["detail"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_retry_class_premise_failure_halts_as_reviewer_unready():
    halted = _eval({"pulled": [{"reason": "prep gate error -32004: hung_past_ceiling"}]})
    assert halted["halted"] == HALT_REVIEWER_UNREADY and halted["retryable"] is True
    assert halted["reason"].startswith(NO_READY_LITERAL + ": prep gate error -32004")
    _assert_refire(halted)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_regate_blitz_is_read_too():
    halted = _eval({"pulled": [], "regate": {"agentIncidents": [{"role": "readiness-host:1", "kind": "missing-tldr"}], "pulled": []}})
    assert halted["halted"] == HALT_REVIEWER_UNREADY


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_bare_no_ready_refusal_still_carries_the_refire():
    halted = _eval({"agentIncidents": [], "pulled": [{"reason": "1 blocking, 0 advisory: a"}]})
    assert halted["halted"] == HALT_REFUSAL and "retryable" not in halted
    assert halted["reason"] == f"{NO_READY_LITERAL}: 1 blocking, 0 advisory: a"
    _assert_refire(halted)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_an_unrelated_incident_does_not_type_the_halt():
    halted = _eval({"agentIncidents": [{"role": "reviewer", "kind": "roster-unreadable"}, {"role": "scout", "kind": "errored"}], "pulled": []})
    assert halted["halted"] == HALT_REFUSAL and halted["reason"] == NO_READY_LITERAL


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_pin_drift_repin_keeps_precedence_over_the_typed_halt():
    pulled = [{"planPath": "docs/plans/p.md", "reason": "register-pin-drift -32004"}]
    halted = _eval({"agentIncidents": [{"role": "reviewer", "kind": "missing-tldr"}], "pulled": pulled})
    assert halted["halted"] == HALT_REFUSAL
    assert halted["next_action"]["input"]["legacy_kind"] == "repin_register"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_pull_arm_keeps_precedence_over_the_typed_halt():
    pre = "_pullPlan = 'docs/plans/p.md'; _pullBlocking = 1;\n"
    halted = _eval({"agentIncidents": [{"role": "reviewer", "kind": "missing-tldr"}], "pulled": []}, pre)
    assert halted["halted"] == HALT_REFUSAL
    assert halted["next_action"]["input"]["legacy_kind"] == "rule_and_refire"
