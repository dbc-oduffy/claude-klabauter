"""The composed ledger-resume block runs the re-plan leg for unserved register rows at every arm.

These pin the emitted JS text only; the script is not executed here, so the runtime halt is unproven."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import RESUME_LEDGER_UNSERVED_FIELD
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_UNSERVED = f"_gate.resume_ledger.{RESUME_LEDGER_UNSERVED_FIELD}"


def _script(monkeypatch, arm: str) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
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


@pytest.mark.parametrize("arm", ["s", "xs"])
def test_ledger_unserved_is_arm_agnostic_and_releases_the_owes_predicate(monkeypatch, arm):
    script = _script(monkeypatch, arm)
    unserved = f"const _ledgerUnserved = _resumeLedger && (({_UNSERVED} ?? []).length > 0);"
    assert unserved in script
    assert "'m_plus'" not in unserved
    owes = script[script.index("const _ledgerOwes"):]
    owes = owes[: owes.index(";\n")]
    assert owes.startswith("const _ledgerOwes = _resumeLedger && _gate.arm === 'm_plus' && (")
    assert owes.endswith(") || _ledgerUnserved")
    assert script.index("const _ledgerUnserved") < script.index("const _ledgerOwes")


@pytest.mark.parametrize("arm", ["s", "xs"])
def test_the_replan_prompt_names_unserved_rows_and_requires_register_rows(monkeypatch, arm):
    script = _script(monkeypatch, arm)
    assert f"JSON.stringify({_UNSERVED} ?? [])" in script
    assert "Every row you add carries register_rows naming the register id it serves" in script
    assert "the landing fence admits the writes of serving rows" in script


@pytest.mark.parametrize("arm", ["s", "xs"])
def test_a_replan_that_adds_no_row_halts_instead_of_reaching_ledger_check(monkeypatch, arm):
    script = _script(monkeypatch, arm)
    halt = "if (_ledgerUnserved) { _halted = { halted: "
    assert halt in script
    tail = script[script.index(halt):]
    tail = tail[: tail.index("run_id: _runId }")]
    assert "unserved register row(s) " in tail
    assert "re-size the sizing, or fire a fresh ask whose asks[].writes cover the work" in tail
    assert script.index("_ledgerAdded.length) { _ledgerExec = true;") < script.index(halt)


def test_the_m_plus_prefix_of_the_owes_predicate_is_unchanged(monkeypatch):
    script = _script(monkeypatch, "s")
    assert "const _ledgerOwes = _resumeLedger && _gate.arm === 'm_plus' && (((" in script
