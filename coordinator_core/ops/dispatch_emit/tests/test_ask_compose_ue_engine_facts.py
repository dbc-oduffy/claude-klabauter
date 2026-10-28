"""compose_ask_script carries the UE engine-facts clause in plan-author, executor and review briefs only when served."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.ue_engine_facts import CLAUSE

_MARK = "your training data on UE5 APIs is stale"
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _compose():
    return compose_ask_script(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        run_base_sha="a" * 40,
        **REVIEW_KW,
    )


def test_served_repo_briefs_carry_the_clause_in_each_role(monkeypatch):
    monkeypatch.setattr(ask_compose, "ue_engine_facts_clause", lambda root: CLAUSE)
    script = _compose()
    plan_author = script.split("Author the plan for the sizing at ", 1)[0]
    assert _MARK in plan_author[-4000:]
    row_prompt = script.split("const _rowPrompt = ", 1)[1].split("const _groups", 1)[0]
    assert _MARK in row_prompt
    assert script.count(_MARK) >= 3
    assert "project_engine_list_modules" in script


def test_unserved_repo_briefs_carry_no_clause(monkeypatch):
    monkeypatch.setattr(ask_compose, "ue_engine_facts_clause", lambda root: "")
    script = _compose()
    assert _MARK not in script
    assert "project_engine_session_primer" not in script


def test_unserved_compose_omits_every_clause_line(monkeypatch):
    monkeypatch.setattr(ask_compose, "ue_engine_facts_clause", lambda root: "")
    script = _compose()
    assert "project_engine_list_modules" not in script
    assert "validate_ue_api" not in script


def test_real_reader_on_a_bare_repo_yields_no_clause():
    # "REPO" has no example-retrieval-repo index, so the real reader answers "" and nothing is emitted.
    assert _MARK not in _compose()
