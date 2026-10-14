"""The composed ask script hands withheld rows' owner-repo roots to the ledger checker and the delivery verifier."""

from __future__ import annotations

import copy

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import _V5_ROSTER_FRAGMENT, _V5_STAGE_SCHEMAS
from coordinator_core.ops.review_mint import execute_review as er

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_ROSTER = copy.deepcopy(_V5_ROSTER_FRAGMENT)
_ROSTER["execute_review"]["stages"][1]["agents"].append(
    {"agentType": "coordinator:delivery-verifier", "model": "opus", "effort": "low", "per": "whole-diff", "schema": "wave"}
)
_CLAUSE = er.owner_repo_clause_js("_crossRepoRows")


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
        review_roster_fragment=_ROSTER,
        review_stage_schemas=_V5_STAGE_SCHEMAS,
    )


def test_binding_is_declared_then_set_from_gated_rows_after_stage(monkeypatch):
    script = _script(monkeypatch)
    decl = script.index("let _crossRepoRows = [];")
    stage = script.index("label: 'stage'")
    assign = script.index("_crossRepoRows = (_manifest.gated ?? [])")
    assert decl < stage < assign
    assert "owner_root: g.owner_root ?? ''" in script


def test_manifest_schema_carries_owner_root():
    assert '"owner_root"' in str(ask_compose._MANIFEST_SCHEMA).replace("'", '"')


def test_ledger_check_and_delivery_verifier_both_carry_the_clause(monkeypatch):
    script = _script(monkeypatch)
    ledger_start = script.index("phase: ledger-check")
    ledger = script[ledger_start : script.index("label: 'ledger-check'", ledger_start)]
    assert _CLAUSE in ledger
    assert script.count(_CLAUSE) >= 2
    assert "_crossRepoRows" in script[script.index("Review wave") :]


def test_clause_names_git_c_owner_root_and_unreadable_fallback():
    assert "git -C {root}" in er._OWNER_OWNED
    assert "Deliverable-Id" in er._OWNER_CLOSURE
    assert "unreadable" in er._OWNER_ABSENT and "never report not_met" in er._OWNER_ABSENT


def test_compose_execute_review_keyword_is_opt_in():
    import inspect

    default = inspect.signature(er.compose_execute_review).parameters["owner_rows_js"].default
    assert default is None
