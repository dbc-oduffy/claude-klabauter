"""The M+ ledger-replan agent's stage call carries ledger_resume, its prompt names the credited_landed
items as landed, and the plan-blitz stage read never runs on a ledger resume. Asserts on composed text."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import RESUME_LEDGER_CREDITED_FIELD
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _script(monkeypatch, arm="m_plus") -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel="state/sizings/x.yaml", run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="x.mjs", in_run_commit=True, **REVIEW_KW,
    )


def _replan_prompt(script: str) -> str:
    end = script.index("label: 'ledger-replan'")
    return script[script.rindex("Ledger-resume re-plan leg", 0, end):end]


def test_replan_stage_params_carry_ledger_resume(monkeypatch):
    replan = _replan_prompt(_script(monkeypatch))
    assert "only_rows" in replan and "ledger_resume: true" in replan


def test_replan_prompt_names_credited_landed_items(monkeypatch):
    replan = _replan_prompt(_script(monkeypatch))
    assert RESUME_LEDGER_CREDITED_FIELD in replan and "never re-plan" in replan


def test_blitz_stage_read_is_skipped_on_a_ledger_resume(monkeypatch):
    script = _script(monkeypatch)
    guard = "_planRel && !_resumeLedger && !_stagedManifest"
    assert script.count(guard) == script.count("_blitzStageRead(); }") >= 1


def test_the_non_ledger_only_rows_resume_never_gains_ledger_resume(monkeypatch):
    script = _script(monkeypatch)
    assert "only_rows: _gate.resume_rows" in script
    assert "ledger_resume: _gate" not in script and "...(_gate.resume_plan && _gate.resume_rows ? { only_rows: _gate.resume_rows, ledger_resume" not in script


def test_xs_arm_resume_ledger_branch_is_unchanged(monkeypatch):
    script = _script(monkeypatch, arm="s")
    assert "if (_resumeLedger) { /* planRel already the ledger plan */ }" in script
