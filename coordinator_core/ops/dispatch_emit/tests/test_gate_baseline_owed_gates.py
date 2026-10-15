"""The gate-baseline helper is declared whenever owed gates exist, including gates carried from
a prior run while the current rows carry none."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit.emit import compose_script, derive_plan_context
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.session.record_homes import record_path

from .conftest import REVIEW_KW

_GATE = "row_build_gate:\n  - when: {surface_glob: '**/*.ts'}\n    command: 'tsc --noEmit'\n"


def _ctx():
    text = f"---\ntitle: P\n{_GATE}---\n\n# P\n\n## Goal\n\nG.\n\n## Tasks\n\nx\n"
    return derive_plan_context(text, fallback_title="p")


def _row(row_id, path):
    return WaveRow(
        id=row_id, title=f"t-{row_id}", surface="s", writes=[path], reads=[],
        depends_on=[], body=f"Spec: docs/plans/p.md ({row_id})\nSummary: x.\n",
    )


def _compose(rows, **kw):
    return compose_script(
        [rows], name="wf", description="d",
        plan_path=Path(record_path(".", "mise-inventory", "x.spine.md")).as_posix(),
        predispatch=False, review_specs=[], plan_context=_ctx(), **REVIEW_KW, **kw,
    )


def test_carried_owed_gates_declare_the_helper_when_current_rows_are_ungated():
    # owed_gates ({"Z": [...]}) is non-empty while the distinct gate commands of the
    # current rows (a.py matches no gate) are empty: the two sets differ.
    script = _compose([_row("A", "a.py")], carried_gate_owed={"Z": ["tsc --noEmit"]})
    assert "const _gateBase = {};" in script
    assert "function _gateBaselineText" in script
    assert script.count("const _gateBase = {};") == 1


def test_no_owed_gates_and_ungated_rows_emit_no_helper():
    script = _compose([_row("A", "a.py")])
    assert "_gateBaselineText" not in script


def test_gated_rows_declare_the_helper_once():
    script = _compose([_row("A", "a.ts")], carried_gate_owed={"Z": ["tsc --noEmit"]})
    assert script.count("const _gateBase = {};") == 1
