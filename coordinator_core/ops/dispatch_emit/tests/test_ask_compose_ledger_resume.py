"""A resume_ledger gate re-judges only the blocking rows and reaches the normal ledger halt and commit."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import _GATE_SCHEMA, compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "s")
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        in_run_commit=True,
        **REVIEW_KW,
    )


def test_the_gate_schema_carries_resume_ledger():
    assert _GATE_SCHEMA["properties"]["resume_ledger"] == {"type": ["object", "null"]}


def test_resume_binds_the_plan_and_takes_the_xs_tail_before_any_plan_or_executor_call(monkeypatch):
    script = _script(monkeypatch)
    bind = script.index("const _resumeLedger = !_halted && !!_gate.resume_ledger;")
    assert "_planRel = _gate.resume_ledger.plan; _gate = { ..._gate, arm: 'xs' }" in script[bind:]
    assert bind < script.index("label: 'plan'")
    execute = script.index("if (!_halted && !_resumeLedger) {\n  phase('Execute');")
    assert bind < execute < script.index("label: 'verify'")


def test_resume_composes_no_review_or_test_call_before_verify(monkeypatch):
    script = _script(monkeypatch)
    review = script.index("if (!_halted && _gate.arm !== 'xs') {")
    assert script.index("_gate = { ..._gate, arm: 'xs' }") < review < script.index("label: 'verify'")


def test_the_verify_prompt_names_exactly_the_blocking_ids(monkeypatch):
    script = _script(monkeypatch)
    assert "_gate.resume_ledger.blocking.join(', ')" in script
    assert "Return an item for each of them and no other item." in script
    assert script.index("_gate.resume_ledger.blocking.join") < script.index("label: 'verify'")


def test_the_ledger_halt_still_precedes_the_commit_on_the_resume_path(monkeypatch):
    script = _script(monkeypatch)
    halt = script.index("halted: 'ledger-check'")
    assert script.index("label: 'verify'") < halt < script.index("phase('commit')")


def test_the_gate_reply_prompts_name_resume_ledger(monkeypatch):
    assert "resume_rows, resume_ledger and spine_plan" in _script(monkeypatch)
