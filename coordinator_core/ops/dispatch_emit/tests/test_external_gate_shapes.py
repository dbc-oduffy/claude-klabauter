"""End-to-end coverage for claude-klabauter#43: two `external_gate` shapes
that ``read_spine`` now refuses/withholds, exercised through the REAL
production emit path (``emit.emit_script``), not a hand-built dict.

``coordinator_core.ops.dispatch_emit.spine_read.read_spine`` already carries
unit coverage for both shapes (``test_spine_read_external_gate_shapes.py``),
but that coverage stops at ``read_spine``'s own return value. This module
closes the defined-but-unwired risk named in the dispatch brief: it drives a
full plan file through ``emit_script`` -- the same function
``coordinator_core/ops/dispatch_emit/emit.py``'s only public entry point
calls ``read_spine`` from (line ~3875) -- and asserts the gated row never
reaches an emitted wave, and is named in the "ROWS THIS SCRIPT DOES NOT RUN"
header rather than silently vanishing.

Shape 1 -- the gate is declared in plan FRONTMATTER carrying `row: <id>`.
Shape 2 -- the gate is a row-level sequence of plain strings, not mappings.
"""

from __future__ import annotations

import textwrap

from coordinator_core.ops.dispatch_emit.emit import emit_script

_HEADER_TEMPLATE = textwrap.dedent(
    """\
    ---
    title: "Fixture plan"
    created: 2026-08-13
    author: test
    status: draft
    branch: "work/fixture"
    plan_id: "pln-fixture"
    deliverable_id: "dlv-fixture"
    initiative: null
    sizing_object: "state/sizings/fixture.yaml"
    scope_mode: feature
    problem_set: inline
    {extra_frontmatter}---

    # Fixture plan

    ## Tasks

    ```yaml plan-tasks
    {body}
    ```
    """
)


def _write_plan(tmp_path, body: str, *, extra_frontmatter: str = ""):
    path = tmp_path / "fixture-plan.md"
    path.write_text(
        _HEADER_TEMPLATE.format(extra_frontmatter=extra_frontmatter, body=body),
        encoding="utf-8",
    )
    return path


def _two_row_body(gated_row_extra: str = "") -> str:
    return (
        "- id: T1b\n"
        "  title: gated row\n"
        "  change_kind: code-edit\n"
        "  surface: coordinator_core/ops/dispatch_emit/spine_read.py\n"
        "  writes:\n"
        "    - coordinator_core/ops/dispatch_emit/spine_read.py\n"
        "  reads: []\n"
        "  queue_scope: project\n"
        "  disposition: open\n"
        f"{gated_row_extra}"
        "  body: |\n"
        "    Fixture body for the gated row.\n"
        "- id: T2\n"
        "  title: ordinary row\n"
        "  change_kind: code-edit\n"
        "  surface: coordinator_core/ops/dispatch_emit/wave_map.py\n"
        "  writes:\n"
        "    - coordinator_core/ops/dispatch_emit/wave_map.py\n"
        "  reads: []\n"
        "  queue_scope: project\n"
        "  disposition: open\n"
        "  body: |\n"
        "    Fixture body for the ordinary row.\n"
    )


def test_frontmatter_row_gate_withholds_the_row_from_every_wave(tmp_path):
    """Shape 1 (issue repro): a frontmatter `external_gate` entry carrying
    `row: T1b` must keep T1b out of the emitted script entirely, and it
    must be named in the "ROWS THIS SCRIPT DOES NOT RUN" header instead of
    silently vanishing."""
    extra_frontmatter = (
        "external_gate:\n"
        "  - id: claude-klabauter-inbox-delivery\n"
        "    row: T1b\n"
        "    owner_repo: claude-klabauter\n"
        "    requires: commit-in-owner-repo\n"
        "    cleared: false\n"
    )
    plan_path = _write_plan(tmp_path, _two_row_body(), extra_frontmatter=extra_frontmatter)

    script = emit_script(plan_path)

    assert "T1b" not in _phase_and_agent_ids(script)
    assert "ROWS THIS SCRIPT DOES NOT RUN" in script
    assert "T1b" in script.split("ROWS THIS SCRIPT DOES NOT RUN", 1)[1].split(
        "\n\n", 1
    )[0]
    assert "T2" in script


def test_row_level_plain_string_gate_list_withholds_the_row_from_every_wave(tmp_path):
    """Shape 2 (issue repro): a row-level `external_gate` sequence of plain
    strings (not mappings) must gate that row the same as a well-formed
    mapping with `cleared: false`."""
    gated_row_extra = (
        "  external_gate:\n"
        "    - host-retrieval-runtime\n"
        "    - bank-compatible-engine-store\n"
    )
    plan_path = _write_plan(tmp_path, _two_row_body(gated_row_extra))

    script = emit_script(plan_path)

    assert "T1b" not in _phase_and_agent_ids(script)
    assert "ROWS THIS SCRIPT DOES NOT RUN" in script
    assert "T2" in script


def _phase_and_agent_ids(script: str) -> str:
    """Return the emitted `meta.phases` bracket contents plus every wave
    body -- i.e. everything BUT the "ROWS THIS SCRIPT DOES NOT RUN" header
    -- so a row id's appearance there, and only there, is unambiguous
    evidence it was actually scheduled into a wave rather than merely
    mentioned in passing."""
    if "ROWS THIS SCRIPT DOES NOT RUN" in script:
        return script.split("ROWS THIS SCRIPT DOES NOT RUN", 1)[0]
    return script
