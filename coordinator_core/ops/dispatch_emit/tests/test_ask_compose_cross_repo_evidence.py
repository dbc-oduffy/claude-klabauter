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
    stage = script.index("if (_stagedManifest) { const _thin")
    assign = script.index("_crossRepoRows = (_manifest.gated ?? [])")
    assert decl < stage < assign
    assert "owner_root: g.owner_root ?? ''" in script


def test_manifest_schema_carries_owner_root():
    assert '"owner_root"' in str(ask_compose._MANIFEST_SCHEMA).replace("'", '"')


def test_verify_leg_and_delivery_verifier_both_carry_the_clause(monkeypatch):
    script = _script(monkeypatch)
    verify_start = script.index("phase: Verify")
    verify = script[verify_start : script.index("label: 'verify'", verify_start)]
    assert _CLAUSE in verify
    assert script.count(_CLAUSE) >= 2
    assert "_crossRepoRows" in script[script.index("Review wave") :]


def test_engine_rows_join_the_cross_repo_rows_after_stage(monkeypatch):
    script = _script(monkeypatch)
    assign = script.index("_crossRepoRows = (_manifest.gated ?? [])")
    assert ".concat(_manifest.engine_rows ?? [])" in script[assign : assign + 400]
    assert "engine_rows" in str(ask_compose._MANIFEST_SCHEMA)
    assert "engine_rows" not in ask_compose._MANIFEST_SCHEMA["required"]


def test_ledger_schema_and_prompt_ask_for_root_read(monkeypatch):
    script = _script(monkeypatch)
    item = ask_compose._LEDGER_CHECK_SCHEMA["properties"]["items"]["items"]
    assert "root_read" in item["properties"]
    assert "root_read" not in item["required"]
    verify_start = script.index("phase: Verify")
    assert "root_read" in script[verify_start : script.index("label: 'verify'", verify_start)]


def test_criterion_judge_expression_carries_the_owner_clause(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    roster = copy.deepcopy(_ROSTER)
    roster["execute_review"]["stages"].append(
        {"kind": "judge", "agents": [{"agentType": "coordinator:criterion-judge", "model": "opus", "effort": "low", "schema": "judge"}]}
    )
    script = compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel="state/sizings/x.yaml", run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub", script_path="x.mjs",
        review_roster_fragment=roster, review_stage_schemas={**_V5_STAGE_SCHEMAS, "judge": {"type": "object"}},
    )
    assert "agentType: 'coordinator:criterion-judge'" not in script
    assert "label: 'ledger-check'" not in script
    verify = script[: script.index("label: 'verify'")]
    verify = verify[verify.rindex("agent("):]
    assert "Ledger check, folded" in verify and "root_read" in verify and "engine-plane" in verify
    assert script.count(_CLAUSE) >= 2


def test_clause_names_git_c_owner_root_and_unreadable_fallback():
    assert "git -C {root}" in er._OWNER_OWNED
    assert "Deliverable-Id" in er._OWNER_CLOSURE
    assert "unreadable" in er._OWNER_ABSENT and "never report not_met" in er._OWNER_ABSENT


def test_compose_execute_review_keyword_is_opt_in():
    import inspect

    default = inspect.signature(er.compose_execute_review).parameters["owner_rows_js"].default
    assert default is None
