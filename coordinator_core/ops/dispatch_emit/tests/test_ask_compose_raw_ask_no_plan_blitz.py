"""A raw or intake-sized ask embeds no plan-blitz; an M+ sizing does, so the re-fire terminates."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
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


def test_raw_ask_m_plus_pick_halts_naming_the_sizing_refire():
    script = _compose()
    assert "the sizing resolved to M+, and this script does not embed the plan-blitz stage" in script
    assert _REFIRE in script
    assert script.count("does not embed the plan-blitz stage") == 1
