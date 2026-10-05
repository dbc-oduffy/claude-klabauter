"""Plan ``## Execution notes`` and frontmatter ``row_build_gate`` reach row prompts."""

from coordinator_core.ops.dispatch_emit.emit import (
    _row_prompt,
    derive_plan_context,
)
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

_PLAN = "docs/plans/p.md"


def _row(rid, writes, change_kind=None):
    return WaveRow(
        id=rid, title="t", surface=writes[0], writes=writes, reads=[],
        depends_on=[], change_kind=change_kind,
    )


def _ctx(frontmatter_extra="", notes=None):
    text = f"---\ntitle: P\n{frontmatter_extra}---\n\n# P\n\n## Goal\n\nG.\n\n"
    if notes is not None:
        text += f"## Execution notes\n\n{notes}\n\n"
    text += "## Tasks\n\nx\n"
    return derive_plan_context(text, fallback_title="p")


_GATE = (
    "row_build_gate:\n"
    "  - when: {surface_glob: 'Source/**/*.cpp'}\n"
    "    command: 'build.bat Editor'\n"
    "  - when: {change_kind: doc-edit}\n"
    "    command: 'echo docs'\n"
)


def test_execution_notes_reach_the_prompt_verbatim():
    ctx = _ctx(notes="Always compile `Foo` first.\n\n- step two")
    prompt = _row_prompt(_row("C1", ["a.py"]), _PLAN, ctx)
    assert "Always compile `Foo` first.\n\n- step two" in prompt


def test_absent_notes_add_nothing():
    prompt = _row_prompt(_row("C1", ["a.py"]), _PLAN, _ctx())
    assert "Execution notes" not in prompt


def test_notes_are_size_capped():
    ctx = _ctx(notes="x" * 9000)
    assert len(ctx.execution_notes) <= 4000


def test_matching_row_gets_the_gate_step():
    ctx = _ctx(_GATE)
    prompt = _row_prompt(_row("C1", ["Source/Mod/a.cpp"]), _PLAN, ctx)
    assert "Build gate (mandatory)" in prompt
    assert "`build.bat Editor`" in prompt
    assert "PARTIAL" in prompt
    assert "echo docs" not in prompt


def test_change_kind_gate_matches():
    ctx = _ctx(_GATE)
    prompt = _row_prompt(_row("C2", ["a.md"], "doc-edit"), _PLAN, ctx)
    assert "`echo docs`" in prompt
    assert "build.bat" not in prompt


def test_non_matching_row_gets_no_gate():
    ctx = _ctx(_GATE)
    prompt = _row_prompt(_row("C3", ["tools/a.py"], "code-edit"), _PLAN, ctx)
    assert "Build gate" not in prompt


def test_malformed_gate_is_tolerated():
    ctx = _ctx("row_build_gate:\n  - {when: {}}\n  - nonsense\n")
    assert ctx.row_build_gates == ()
    ctx = _ctx("row_build_gate: not-a-list\n")
    assert ctx.row_build_gates == ()
