"""ask_compose size stage: research flags ride the research conditional and add no agent call."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RESEARCH_CLASSIFY_CLAUSE,
    SCOUT_SCHEMA,
)
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _compose() -> str:
    return ask_compose.compose_ask_script(
        repo_root="REPO",
        prompt="Survey how the sizing gate routes asks and write up the findings",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=_stub_wrap,
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def _line(script: str, needle: str) -> str:
    return next(ln for ln in script.splitlines() if needle in ln)


def test_reassemble_research_flags_sit_under_the_research_conditional():
    cmd = _line(_compose(), "const _reassembleCmd")
    before, _, after = cmd.partition("_asmFields.research !== null")
    assert "--research-" not in before
    assert "--research-class" in after and ": '')" in after


def test_grind_scout_schema_has_no_research_key():
    assert "research" not in SCOUT_SCHEMA["properties"]


def test_classification_adds_no_agent_call(monkeypatch):
    with_clause = _compose()
    monkeypatch.setattr(ask_compose, "RESEARCH_CLASSIFY_CLAUSE", "")
    stripped = _compose()
    assert RESEARCH_CLASSIFY_CLAUSE not in stripped
    assert with_clause.count("agent_type:") == stripped.count("agent_type:")
    assert with_clause.count("label:") == stripped.count("label:")
