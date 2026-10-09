"""compose_ask_script accept phase: an in-run APM ruling re-gates; baton threads into size and gate."""

from __future__ import annotations

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_BATON = {"path": "state/handoffs/b.md", "deliverable_id": "dlv-1"}


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


def _accept_section(script: str) -> str:
    start = script.index("phase('accept')")
    return script[start : script.index("if (_gate.halt || !_gate.arm)", start)]


def test_raw_ask_accept_phase_follows_gate():
    script = _compose()
    assert script.index("phase('gate')") < script.index("phase('accept')") < script.index("phase('plan')")
    section = _accept_section(script)
    assert "agentType: 'coordinator:apm'" in section
    assert "sizing.accept_exit_criterion" in section and "apm_ruling" in section
    assert "ruling_ref: _runId" in section
    assert "run_id" not in section
    assert "pm_quote" not in section
    assert "'accept'" in script.split("phases: [", 1)[1].split("]", 1)[0]


def test_pm_only_or_failed_record_leaves_touchpoint_halt():
    script = _compose()
    section = _accept_section(script)
    assert "_apm.verdict === 'ruled'" in section and "_recorded.ok === true" in section
    assert "_halted = { halted: (_gate.halt && _gate.halt.kind)" in script
    assert "halt: { ..._gate.halt, apm:" in section


def test_sizing_entry_without_pending_has_no_accept_phase():
    script = _compose(prompt=None, sizing_rel="state/sizings/x.yaml")
    assert "phase('accept')" not in script
    assert "'accept'" not in script.split("phases: [", 1)[1].split("]", 1)[0]


def test_accept_pending_composes_accept_for_existing_sizing():
    script = _compose(prompt=None, sizing_rel="state/sizings/x.yaml", accept_pending=True)
    assert "phase('accept')" in script
    assert "'accept'" in script.split("phases: [", 1)[1].split("]", 1)[0]


def test_baton_threads_into_size_and_gate():
    with_baton = _compose(baton=_BATON)
    assert "--deliverable-id dlv-1" in with_baton
    assert 'baton: "state/handoffs/b.md"' in with_baton
    plain = _compose()
    assert "--deliverable-id" not in plain and "baton: \"" not in plain


def test_contract_linter_stays_clean():
    for kw in ({}, {"baton": _BATON}, {"prompt": None, "sizing_rel": "state/sizings/x.yaml", "accept_pending": True}):
        findings = run_checks(_compose(**kw))
        assert not [f for f in findings if f.severity == Severity.ERROR], findings
