"""ask_compose size stage: the in-run scout returns design_route and the assemble command passes it."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose, design_route
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_SCOUT_SCHEMA,
    DESIGN_ROUTE_CLAUSE,
    SCOUT_SCHEMA,
    design_route_assemble_flags_js,
)
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _compose() -> str:
    return ask_compose.compose_ask_script(
        repo_root="REPO",
        prompt="Design the new dashboard",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=_stub_wrap,
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def test_schema_admits_one_optional_design_route():
    prop = ASK_SCOUT_SCHEMA["properties"]["design_route"]
    assert prop["enum"] == list(design_route.ROUTES)
    assert "design_route" not in ASK_SCOUT_SCHEMA["required"]


def test_grind_scout_schema_is_untouched():
    assert "design_route" not in SCOUT_SCHEMA["properties"]


def test_helper_emits_the_flag_only_for_a_known_route():
    expr = design_route_assemble_flags_js("s", "q")
    assert expr.startswith(" + ")
    assert "--design-route" in expr
    assert "['canvas', 'reviewer', 'none'].includes(s.design_route)" in expr
    assert expr.endswith(": '')")


def test_composed_script_carries_flag_clause_and_template():
    script = _compose()
    assert design_route_assemble_flags_js("_asmFields", "_q") in script
    assert DESIGN_ROUTE_CLAUSE in script
    assert "a-large-design-ask-goes-to-the-canvas.md" in script
    assert "[--design-route <design_route> when design_route is returned]" in script


def test_grind_scout_text_has_no_design_route():
    from coordinator_core.ops.dispatch_emit import grind_stages

    assert "--design-route" not in grind_stages.__dict__.get("SCOUT_PROMPT", "")
    assert "design_route" not in repr(SCOUT_SCHEMA)
