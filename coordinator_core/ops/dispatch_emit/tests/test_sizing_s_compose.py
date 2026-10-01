"""compose_s_stage: phases, fixed plan path, --plan hand-off, plan-only terminal marker."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.commit_request import parse_marker
from coordinator_core.ops.dispatch_emit.sizing_fire import S_STAGE1_PHASES, s_plan_path
from coordinator_core.ops.dispatch_emit.sizing_s_compose import compose_s_stage

SIZING_REL = "state/sizings/2026-10-01-an-s-job.yaml"
SIZING = {
    "intent": "Add the widget `frobnicate` ${x}",
    "exit_criterion": {"statement": "Widgets frobnicate end to end", "accepted": "yes"},
    "interaction_mode": "unattended",
}
ROOT = "/fixture/repo"
SESSION ="d7b9dc1a-1455-43e8-922f-87e734b5634e"


def _text() -> str:
    return compose_s_stage(SIZING, sizing_rel=SIZING_REL, repo_root=ROOT, session_id=SESSION)


def test_phases_in_order():
    text = _text()
    pos = [text.index(f"phase('{p}')") for p in S_STAGE1_PHASES[:2]]
    assert pos == sorted(pos)
    assert "coordinator:plan-author" in text and "coordinator:executor" in text


def test_names_fixed_plan_path_and_exit_criterion():
    text = _text()
    plan = s_plan_path(SIZING_REL)
    assert plan in text
    assert "Widgets frobnicate end to end" in text
    assert "unattended" in text
    assert "--sizing-object " + SIZING_REL in text
    assert "scope_mode: spec-dispatch" in text


def test_handoff_uses_plan_never_sizing():
    text = _text()
    assert f"emit-dispatch-workflow --plan {s_plan_path(SIZING_REL)} --fire" in text
    assert "--repo-root" not in text
    assert ROOT not in text.split("terminal-commit", 1)[0]
    assert "emit-dispatch-workflow --sizing" not in text
    assert "plan-spine-check" in text


def test_template_literal_escapes_hold():
    text = _text()
    assert "\\`frobnicate\\` \\${x}" in text


def test_marker_declares_only_the_plan_doc():
    req = parse_marker(_text())
    assert req is not None
    paths = [p for c in req.chunks for p in c.paths]
    assert paths == [s_plan_path(SIZING_REL)]
    assert all(not c.prefixes for c in req.chunks)
