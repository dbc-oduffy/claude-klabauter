"""Pins the gate_v2 contract: requirement-9 key sets, refusal type, fail-closed stub."""
import ast
from pathlib import Path

import pytest

from coordinator_core.roadmap import gate_v2

_STRUCK = {
    "planning_wave", "shared_wave_slot", "scanned", "gated", "replanned", "resurrected",
    "superseded", "deliverable_id_collisions", "inert_fields", "kind", "roadmap_id",
    "blocked_by", "tracked", "unresolved_sizings", "hold_cleared", "index_unreadable",
}


def test_baton_keys_equal_requirement_9():
    assert gate_v2.BATON_REPORT_KEYS == frozenset(
        "id path title stub_id status deployment_state candidate plan needs_plan "
        "execution_authorized sized sizing_objects planning_gate execution_gate "
        "unlinked_plan_claim ambiguous_plan_link held waiting_on_execution".split()
    )


def test_report_keys_equal_requirement_9():
    assert gate_v2.REPORT_KEYS == frozenset(
        "batons waves cycles unresolved_blockers untracked held counts "
        "unmatched_targets verdict".split()
    )


def test_no_struck_field_in_either_key_set():
    assert not (_STRUCK & gate_v2.BATON_REPORT_KEYS)
    assert not (_STRUCK & gate_v2.REPORT_KEYS)


def test_stub_refuses_closed(tmp_path):
    with pytest.raises(gate_v2.GateReadRefused):
        gate_v2.compute_gate_report(tmp_path)
    with pytest.raises(gate_v2.GateReadRefused):
        gate_v2.compute_gate_report(tmp_path, roadmap_id="r", targets=["a"], subject="s")


def test_module_imports_no_subprocess_and_no_op():
    tree = ast.parse(Path(gate_v2.__file__).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    assert not any(n.split(".")[0] == "subprocess" for n in names)
    assert not any("roadmap_plan_gate" in n for n in names)
