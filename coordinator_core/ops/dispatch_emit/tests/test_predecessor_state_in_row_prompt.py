"""Live predecessor-handoff state reaches every row prompt."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.emit import _row_prompt
from coordinator_core.ops.dispatch_emit.predecessor_state import (
    predecessor_state_section,
    resolve_predecessor_state,
)
from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

_STATE = "Chunk C1 WIP is committed."


def _handoff(repo, name, deliverable_id, state=_STATE):
    d = repo / "state" / "handoffs"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(
        f"---\ndeliverable_id: {deliverable_id}\n---\n# H\n\n## Current State\n\n{state}\n\n## Other\n\nno-leak\n",
        encoding="utf-8",
    )


def _plan(front):
    return f"---\n{front}\n---\n# P\n"


def test_deliverable_id_match_renders_current_state(tmp_path):
    _handoff(tmp_path, "a.md", "D-1")
    _handoff(tmp_path, "b.md", "D-2", state="other deliverable")
    block = resolve_predecessor_state(_plan("deliverable_id: D-1"), tmp_path)
    assert _STATE in block
    assert "other deliverable" not in block
    assert "no-leak" not in block


def test_declared_handoff_not_live_is_named(tmp_path):
    plan = _plan("predecessor_handoff: state/handoffs/gone.md")
    assert "is not live" in resolve_predecessor_state(plan, tmp_path)


def test_no_handoff_says_so(tmp_path):
    assert "No live predecessor handoff" in resolve_predecessor_state(_plan("title: x"), tmp_path)


def test_section_is_appended_to_the_row_prompt(tmp_path):
    _handoff(tmp_path, "a.md", "D-1")
    section = predecessor_state_section(_plan("deliverable_id: D-1"), tmp_path)
    row = WaveRow(id="C1", title="t", surface="s", writes=UNDECLARED, reads=[], depends_on=[])
    prompt = _row_prompt(row, "docs/plans/p.md", None, predecessor_state=section)
    assert "## Predecessor handoff state" in prompt
    assert prompt.rstrip().endswith(_STATE)
    assert "## Predecessor handoff state" not in _row_prompt(row, "docs/plans/p.md", None)


def test_no_plan_or_root_yields_no_section(tmp_path):
    assert predecessor_state_section("", tmp_path) is None
    assert predecessor_state_section(_plan("a: b"), None) is None


def test_emit_script_carries_the_handoff_state_into_the_emitted_prompt(tmp_path):
    from coordinator_core.ops.dispatch_emit.emit import emit_script

    from .conftest import REVIEW_KW
    from .test_emit_script_opens_plan_once import _PLAN_TEXT

    _handoff(tmp_path, "a.md", "D-9")
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(
        _PLAN_TEXT.replace("sizing_object: null", "sizing_object: null\ndeliverable_id: D-9", 1),
        encoding="utf-8",
    )
    script = emit_script(plan_path, repo_root=tmp_path, **REVIEW_KW)
    assert "Predecessor handoff state" in script
    assert _STATE in script
