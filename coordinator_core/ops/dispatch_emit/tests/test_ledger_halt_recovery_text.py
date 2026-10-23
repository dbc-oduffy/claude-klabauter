"""The ledger-check halt's next_action names workflow.revise, the run id and the stating scope."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.halted_run_route import ROUTE_OP
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


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


def _halt_text(script: str) -> str:
    start = script.index("halted: 'ledger-check'")
    return script[start : script.index("}; }", start)]


def test_ledger_halt_names_revise_run_id_and_scope(monkeypatch):
    text = _halt_text(_script(monkeypatch))
    assert "coordinator-invoke workflow.revise" in text
    assert "_runId" in text
    assert "`verbatim` carries the PM words ruling them out, recorded as the ruling and adding no row" in text
    assert "The re-fire resumes at the ledger check." in text
    assert "note stating scope" not in text
    assert "verbatim stating scope" not in text
    assert "_ledgerBlocking(_ledger)" in text
    assert "or fix the change" not in text


def test_ledger_halt_names_landed_close_route_in_both_rulable_branches(monkeypatch):
    text = _halt_text(_script(monkeypatch))
    assert text.count(f"'{ROUTE_OP}'") == 2
    assert text.count("{run_id: ' + _runId + ', sha: <the halt commit>}.") == 2
