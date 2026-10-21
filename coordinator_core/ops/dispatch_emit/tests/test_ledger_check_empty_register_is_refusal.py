"""The composed ask script halts an empty register at the verify leg as a refusal naming the register, ahead of the ledger-check halt."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import _lit, compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import (
    EMPTY_REGISTER_REASON,
    EMPTY_REGISTER_REFUSAL_HEAD,
    EMPTY_REGISTER_REFUSAL_TAIL,
    HALT_REFUSAL,
)
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_GUARD = (
    "_emptyVerdict && Array.isArray(_ledgerIds) && !_ledgerIds.length "
    "&& Array.isArray(_withheldIds) && !_withheldIds.length"
)


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        **REVIEW_KW,
    )


def _refusal_statement(script: str) -> str:
    start = script.index(_GUARD)
    return script[start : script.index("}; }", start)]


def test_the_refusal_statement_precedes_the_ledger_check_halt(monkeypatch):
    script = _script(monkeypatch)
    assert script.index(_GUARD) < script.index("halted: 'ledger-check'")


def test_the_refusal_is_guarded_on_an_unset_halt(monkeypatch):
    script = _script(monkeypatch)
    assert "if (!_halted && " + _GUARD in script


def test_the_refusal_carries_the_refusal_kind_and_the_register_text(monkeypatch):
    text = _refusal_statement(_script(monkeypatch))
    assert f"halted: {_lit(HALT_REFUSAL)}" in text
    assert _lit(EMPTY_REGISTER_REASON + EMPTY_REGISTER_REFUSAL_HEAD) + " + _sizingRel + " + _lit(EMPTY_REGISTER_REFUSAL_TAIL) in text
    assert "sizing: _sizingRel" in text and "run_id: _runId" in text


def test_the_ledger_check_halt_clause_is_untouched(monkeypatch):
    script = _script(monkeypatch)
    start = script.index("halted: 'ledger-check'")
    clause = script[start : script.index("}; }", start)]
    assert "no ledger row ids are on record" in clause
    assert "_ledgerIds" not in clause.split("next_action")[0]
