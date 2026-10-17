"""A row with no build gate still learns the outside-footprint report line."""

import dataclasses

from coordinator_core.ops.dispatch_emit.emit import _row_prompt, derive_plan_context
from coordinator_core.ops.dispatch_emit.gate_owed import (
    KIND_OUTSIDE_FOOTPRINT,
    report_line,
)
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

_PLAN = "docs/plans/p.md"
_LINE_PREFIX = report_line(KIND_OUTSIDE_FOOTPRINT, "<first failing test>")
_GATE = (
    "row_build_gate:\n"
    "  - when: {surface_glob: 'Source/**/*.cpp'}\n"
    "    command: 'build.bat Editor'\n"
)


def _row(rid, path):
    return WaveRow(
        id=rid, title="t", surface=path, writes=[path], reads=[], depends_on=[],
    )


def _ctx(extra=""):
    text = f"---\ntitle: P\n{extra}---\n\n# P\n\n## Goal\n\nG.\n\n## Tasks\n\nx\n"
    return derive_plan_context(text, fallback_title="p")


def test_no_gate_row_prompt_carries_the_outside_footprint_line():
    prompt = _row_prompt(_row("C1", "tools/a.py"), _PLAN, _ctx(_GATE))
    assert _LINE_PREFIX in prompt
    assert "Build gate" not in prompt


def test_build_gate_row_carries_the_line_once():
    prompt = _row_prompt(_row("C1", "Source/Mod/a.cpp"), _PLAN, _ctx(_GATE))
    assert "Build gate (mandatory)" in prompt
    assert prompt.count(f"gate-blocker: {KIND_OUTSIDE_FOOTPRINT}:") == 1


def test_slot_row_does_not_gain_the_clause():
    ctx = dataclasses.replace(_ctx(_GATE), slot_row_ids=frozenset({"C1"}))
    prompt = _row_prompt(_row("C1", "Source/Mod/a.cpp"), _PLAN, ctx)
    assert _LINE_PREFIX not in prompt
