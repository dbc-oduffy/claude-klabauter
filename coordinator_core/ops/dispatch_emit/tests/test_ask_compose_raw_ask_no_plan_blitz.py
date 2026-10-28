"""A raw or intake-sized ask embeds no plan-blitz; its M+ leg chains into the {sizing} script; an M+ sizing embeds it."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

ARM_M_PLUS = ask_compose.ARM_M_PLUS

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_DEFINES_BLITZ = "async function planBlitz("
_REFIRE = 'resubmit Workflow({name: "coordinator:engine-ask", args: {sizing: "'


def _compose(**over):
    kw = dict(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


def test_raw_ask_embeds_no_plan_blitz():
    assert _DEFINES_BLITZ not in _compose()


def test_intake_sizing_ask_embeds_no_plan_blitz(monkeypatch):
    monkeypatch.setattr(ask_compose, "_intake_sizing", lambda repo_root, rel: {"estimate": None})
    assert _DEFINES_BLITZ not in _compose(prompt=None, sizing_rel="state/sizings/x.yaml")


def test_m_plus_sizing_ask_embeds_plan_blitz(monkeypatch):
    monkeypatch.setattr(ask_compose, "_intake_sizing", lambda repo_root, rel: None)
    monkeypatch.setattr(ask_compose, "_known_arm", lambda repo_root, rel: ARM_M_PLUS)
    assert _DEFINES_BLITZ in _compose(prompt=None, sizing_rel="state/sizings/x.yaml")


def test_raw_ask_m_plus_leg_emits_and_starts_the_sizing_script_instead_of_halting():
    script = _compose()
    assert "await workflow({ scriptPath: _chainPath })" in script
    assert "dispatch.emit" in script
    assert _DEFINES_BLITZ not in script
    # The refusal survives only as the emit/start failure text.
    assert script.count("does not embed the plan-blitz stage") == 0
    assert "the chained run did not start (" in script
    assert _REFIRE in script


def test_launcher_child_m_plus_leg_returns_a_chain_hand_off_and_starts_no_workflow():
    script = _compose(launcher_child=True)
    assert script.count("does not embed the plan-blitz stage") == 0
    assert f"return {{ {CHAIN_HANDOFF_KEY}: {{ ask: true, sizing_path: _sizingRel" in script
    assert "workflow(" not in script
