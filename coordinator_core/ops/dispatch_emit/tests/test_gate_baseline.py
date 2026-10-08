"""Row-gate baseline: each distinct gate command runs once at run_base before any row, and
its recorded errors reach the gated rows' briefs."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit.emit import compose_script, derive_plan_context
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.session.record_homes import record_path
from coordinator_core.ops._workflow_contract import Severity, run_checks

from .conftest import REVIEW_KW

_GATE = "row_build_gate:\n  - when: {surface_glob: '**/*.ts'}\n    command: 'tsc --noEmit'\n"


def _ctx(frontmatter=_GATE):
    text = f"---\ntitle: P\n{frontmatter}---\n\n# P\n\n## Goal\n\nG.\n\n## Tasks\n\nx\n"
    return derive_plan_context(text, fallback_title="p")


def _row(row_id, path, deps=()):
    return WaveRow(
        id=row_id, title=f"t-{row_id}", surface="s", writes=[path], reads=[],
        depends_on=list(deps), body=f"Spec: docs/plans/p.md ({row_id})\nSummary: x.\n",
    )


def _compose(rows, *, predispatch=True, ctx=None):
    return compose_script(
        [rows], name="wf", description="d",
        plan_path=Path(record_path(".", "mise-inventory", "x.spine.md")).as_posix(),
        predispatch=predispatch, review_specs=[], plan_context=ctx or _ctx(), **REVIEW_KW,
    )


def test_one_baseline_agent_per_distinct_command_before_the_first_row():
    script = _compose([_row("A", "a.ts"), _row("B", "b.ts")])
    assert script.count("label: 'gatebase:") == 1
    assert script.index("label: 'gatebase:0'") < script.index("_rows['A'] = _runRow(")
    assert "Gate baseline. Run this build-gate command ONCE" in script
    assert "row_build_gate red at run_base" in script


def test_gated_row_brief_reads_the_baseline_at_runtime():
    script = _compose([_row("A", "a.ts")])
    assert '_gateBaselineText(["tsc --noEmit"])' in script
    assert "adds no NEW error" in script or "no NEW error" in script
    assert "gate-blocker: outside-footprint" in script


def test_ungated_plan_emits_no_baseline_machinery():
    script = _compose([_row("A", "a.py")])
    assert "gatebase" not in script
    assert "_gateBaselineText" not in script


def test_without_predispatch_the_helper_exists_but_no_baseline_runs():
    script = _compose([_row("A", "a.ts")], predispatch=False)
    assert "const _gateBase = {};" in script
    assert "label: 'gatebase:" not in script


def test_baseline_script_passes_the_workflow_contract_checks():
    findings = run_checks(_compose([_row("A", "a.ts")]))
    assert not [f for f in findings if f.severity == Severity.ERROR], findings
