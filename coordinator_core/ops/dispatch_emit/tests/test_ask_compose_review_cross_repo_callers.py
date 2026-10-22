"""The review and fix prompts composed by compose_ask_script require a sibling-repo caller check before a symbol is deleted."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose, prompt_fragments
from coordinator_core.ops.dispatch_emit.ask_plan_blitz import AskPlanBlitzRefused
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_STUB_KW = dict(
    wrap_stage=lambda _t: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
    plan_blitz_text="stub",
    roadmap_blitz_text="export const meta = { name: 'rb', phases: [{ title: 'Synthesize', detail: 'x' }] };\nreturn {};",
    **REVIEW_KW,
)


def _composed(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    kw = dict(repo_root="REPO", prompt="do a thing", sizing_rel=None, run_id="run-1", session_id=None, script_path="x.mjs")
    try:
        script = ask_compose.compose_ask_script(**kw)
    except AskPlanBlitzRefused:
        script = ask_compose.compose_ask_script(**kw, **_STUB_KW)
    return prompt_fragments.resolve_fragment_refs(script)


def test_review_prompts_carry_the_cross_repo_caller_clause(monkeypatch):
    script = _composed(monkeypatch)
    assert "search the sibling repos for callers" in script
    assert "keep the symbol and report it as a finding" in script


def test_clause_is_part_of_the_review_head(monkeypatch):
    assert ask_compose._CROSS_REPO_CALLER_CLAUSE.strip() in _composed(monkeypatch)
    assert "sibling repos" in ask_compose._CROSS_REPO_CALLER_CLAUSE
