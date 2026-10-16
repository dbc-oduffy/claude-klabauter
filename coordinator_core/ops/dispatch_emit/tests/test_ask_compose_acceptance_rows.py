"""ask_compose size stage: the scout returns acceptance_rows and sizing-assemble gets them as a flag."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ACCEPTANCE_ROWS_CLAUSE,
    acceptance_rows_assemble_flags_js,
)
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _compose() -> str:
    return ask_compose.compose_ask_script(
        repo_root="REPO",
        prompt="Add a flag to the exporter and cover it with a test",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=_stub_wrap,
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def test_scout_prompt_holds_the_clause():
    assert ACCEPTANCE_ROWS_CLAUSE in _compose()


def test_assemble_command_holds_the_helper_expression():
    expr = acceptance_rows_assemble_flags_js("_asmFields", "_q")
    assert expr in _compose()
    assert "--acceptance-rows-json" in expr
    assert "length > 0" in expr


def test_helper_is_empty_unless_rows_are_a_non_empty_array():
    expr = acceptance_rows_assemble_flags_js("s", "q")
    assert expr.startswith(" + ")
    assert expr.endswith(": '')")
    assert "Array.isArray(s.acceptance_rows)" in expr
