"""compose_ask_script: a gate-minted spine takes one plan-review agent and reaches plan-blitz only on failure."""

from __future__ import annotations

import json
import types

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import (
    _GATE_SCHEMA,
    _REVISE_SCHEMA,
    _REVISE_STAGED_SCHEMA,
    _SPINE_REVIEW_SCHEMA,
    _SPINE_REVIEW_STAGED_SCHEMA,
    compose_ask_script,
)
from coordinator_core.ops.dispatch_emit.ask_plan_blitz import AskPlanBlitzRefused
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

pytestmark = pytest.mark.usefixtures("m_plus_known_arm")

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


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


def _m_branch(script):
    start = script.index("_gate.arm === 'm_plus') {")
    return script[start : script.index("\n  }\n", start)]


def test_gate_schema_carries_spine_plan():
    assert "spine_plan" in _GATE_SCHEMA["properties"]


def test_plan_review_agent_is_guarded_by_spine_plan():
    branch = _m_branch(_compose())
    assert "if (_gate.spine_plan && !_gate.resume_plan) {" in branch
    review = branch.index("label: 'plan-review'")
    assert branch.index("_gate.spine_plan && !_gate.resume_plan") < review
    assert "agentType: 'coordinator:staff-eng'" in branch
    assert "phase: 'plan'" in branch[review - 400 : review + 400]
    assert "plan.readiness_verdict" in branch


def test_blitz_is_reached_only_when_the_review_left_no_plan():
    branch = _m_branch(_compose())
    assert branch.index("if (_spineReview?.verdict === 'ready')") < branch.index("if (!_planRel && !_halted) {")
    assert branch.index("if (!_planRel && !_halted) {") < branch.index("await planBlitz(")
    assert "_gate.resume_plan ?? _spineFallback ?? null" in branch


def test_not_ready_review_halts_with_its_verdict_and_skips_blitz():
    branch = _m_branch(_compose())
    assert "reason: 'spine plan review verdict '" in branch
    assert branch.index("if (_spineReview?.verdict === 'ready')") < branch.index("spine plan review verdict")
    assert "if (!_planRel && !_halted) {" in branch
    assert "review: _spineReview" in branch


def _plugin(tmp_path, signals=None, raw=None):
    contract = tmp_path / "contract"
    contract.mkdir()
    text = raw if raw is not None else json.dumps({"signals": signals})
    (contract / "review-signals.json").write_text(text, encoding="utf-8")
    return str(tmp_path)


def _with_contract(monkeypatch, tmp_path, signals=None, raw=None):
    root = _plugin(tmp_path, signals, raw)
    monkeypatch.setattr(
        "coordinator_core.warm.caller_context.resolve_caller_context",
        lambda *a, **k: types.SimpleNamespace(plugin_root=root),
    )


_FIXTURE = {
    "architecture": {"selects": "coordinator:staff-eng"},
    "cross-repo-seam": {"selects": "coordinator:eng-director"},
}


def test_branch_embeds_the_selects_literal_read_from_the_contract(monkeypatch, tmp_path):
    _with_contract(monkeypatch, tmp_path, _FIXTURE)
    branch = _m_branch(_compose())
    literal = json.dumps({s: e["selects"] for s, e in _FIXTURE.items()}, sort_keys=True)
    assert f"const _signalSelects = {literal};" in branch
    assert "review_signals" in branch
    assert "const _signalSelects" not in _compose().split("_gate.arm === 'm_plus') {")[0]


def test_spine_review_schema_carries_review_signals_and_plan_revise_does_not():
    assert "review_signals" in _SPINE_REVIEW_SCHEMA["properties"]
    assert "review_signals" in _SPINE_REVIEW_STAGED_SCHEMA["properties"]
    assert "review_signals" not in _REVISE_SCHEMA["properties"]
    assert "review_signals" not in _REVISE_STAGED_SCHEMA["properties"]


def test_signalled_spine_leads_to_blitz_and_never_stages(monkeypatch, tmp_path):
    _with_contract(monkeypatch, tmp_path, _FIXTURE)
    branch = _m_branch(_compose())
    wrong = branch.index("const _wrongReviewer")
    assert "!== 'coordinator:staff-eng'" in branch[wrong : wrong + 400]
    guard = branch.index("if (!_wrongReviewer) {")
    ready = branch.index("if (_spineReview?.verdict === 'ready')")
    halt = branch.index("spine plan review verdict")
    blitz = branch.index("if (!_planRel && !_halted) {")
    assert wrong < guard < ready < halt < blitz
    assert "_wrongReviewer" not in branch[blitz:]
    assert "_stagedManifest" not in branch[wrong:ready]


def test_architecture_signal_selecting_staff_eng_keeps_the_ready_halt_path(monkeypatch, tmp_path):
    _with_contract(monkeypatch, tmp_path, _FIXTURE)
    branch = _m_branch(_compose())
    assert '"architecture": "coordinator:staff-eng"' in branch
    wrong = branch.index("const _wrongReviewer")
    predicate = branch[wrong : branch.index(";", wrong)]
    assert "!== 'coordinator:staff-eng'" in predicate
    assert "agentType: 'coordinator:staff-eng'" in branch
    assert "spine plan review verdict" in branch[branch.index("if (!_wrongReviewer) {") :]


def test_unknown_signal_is_not_a_wrong_reviewer(monkeypatch, tmp_path):
    _with_contract(monkeypatch, tmp_path, _FIXTURE)
    branch = _m_branch(_compose())
    wrong = branch.index("const _wrongReviewer")
    predicate = branch[wrong : branch.index(";", wrong)]
    assert "_signalSelects[s] !== undefined &&" in predicate


def test_missing_or_unparseable_contract_refuses_by_name(monkeypatch, tmp_path):
    root = tmp_path / "none"
    root.mkdir()
    monkeypatch.setattr(
        "coordinator_core.warm.caller_context.resolve_caller_context",
        lambda *a, **k: types.SimpleNamespace(plugin_root=str(root)),
    )
    with pytest.raises(AskPlanBlitzRefused, match="review-signals.json"):
        _compose()
    bad = tmp_path / "bad"
    bad.mkdir()
    _with_contract(monkeypatch, bad, raw="{not json")
    with pytest.raises(AskPlanBlitzRefused, match="unreadable review-signals.json"):
        _compose()


def test_s_and_xs_arms_carry_no_spine_text():
    script = _compose()
    m_start = script.index("_gate.arm === 'm_plus') {")
    assert "spine_plan" not in script[:m_start].split("_gate.arm === 's')", 1)[-1].split("_gate.arm === 'm_plus'")[0]
    assert "spine-direct" not in script
