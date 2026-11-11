"""`plan-spine-check`'s contradictory-read-declaration and agent-type-undispatchable finding classes."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MOD = Path(__file__).resolve().parents[1] / "plan-spine-check.py"


def _load():
    spec = importlib.util.spec_from_file_location("plan_spine_check_agent_classes", _MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plan_spine_check_agent_classes"] = mod
    spec.loader.exec_module(mod)
    return mod


psc = _load()


class _Malformed(ValueError):
    pass


def _resolve(agent_type, override=None):
    if override == "bad":
        raise _Malformed("agent_model 'bad' is malformed\nsecond line")
    return "sonnet"


_DEFS = {
    "coordinator:executor": {"model": "sonnet"},
    "coordinator:objective-reacher": {"model": "opus", "executes-rows": "false"},
    "coordinator:flagged-bool": {"executes-rows": False},
}


def _classes(rows, defs=_DEFS):
    return psc.agent_type_findings(rows, defs, _resolve, _Malformed)


# --- contradictory-read-declaration ---------------------------------------------------------------


def _overlap(reads, head):
    return sorted(set(reads) & set(head))


def test_path_in_reads_at_head_and_consumes_flagged():
    rows = [{"id": "C1", "consumes": ["a.py"], "reads_at_head": ["a.py", "b.py"]}]
    found = psc.contradictory_read_findings(rows, _overlap)
    assert [f["class"] for f in found] == ["contradictory-read-declaration"]
    assert "'a.py'" in found[0]["detail"] and "row C1" in found[0]["detail"]


def test_path_in_reads_and_reads_at_head_flagged():
    rows = [{"id": "C1", "reads": ["a.py"], "reads_at_head": ["a.py"]}]
    assert len(psc.contradictory_read_findings(rows, _overlap)) == 1


def test_disjoint_declarations_pass():
    rows = [{"id": "C1", "consumes": ["a.py"], "reads_at_head": ["b.py"]}, "not-a-row", {"id": "C2"}]
    assert psc.contradictory_read_findings(rows, _overlap) == []


def test_engine_predicate_agrees():
    from coordinator_core.ops.dispatch_emit import spine_read

    rows = [{"id": "C1", "consumes": ["a.py"], "reads_at_head": ["a.py"]}]
    assert len(psc.contradictory_read_findings(rows, spine_read.contradictory_read_paths)) == 1


# --- agent-type-undispatchable --------------------------------------------------------------------


def test_em_never_dispatchable():
    found = _classes([{"id": "C1", "agent_type": "coordinator:em"}])
    assert [f["class"] for f in found] == ["agent-type-undispatchable"]
    assert "never dispatchable" in found[0]["detail"]


def test_non_executing_definition_flagged_string_and_bool():
    for agent in ("coordinator:objective-reacher", "coordinator:flagged-bool"):
        found = _classes([{"id": "C1", "agent_type": agent}])
        assert len(found) == 1 and "does not execute rows" in found[0]["detail"]


def test_executing_agent_passes():
    assert _classes([{"id": "C1", "agent_type": "coordinator:executor"}]) == []


def test_unknown_coordinator_agent_has_no_definition():
    found = _classes([{"id": "C1", "agent_type": "coordinator:nope"}])
    assert len(found) == 1 and "no agent definition" in found[0]["detail"]


def test_malformed_override_flagged():
    found = _classes([{"id": "C1", "agent_type": "coordinator:executor", "agent_model": "bad"}])
    assert len(found) == 1 and found[0]["detail"] == "row C1: agent_model 'bad' is malformed"


def test_unresolvable_source_yields_only_em_refusal():
    rows = [
        {"id": "C1", "agent_type": "coordinator:objective-reacher"},
        {"id": "C2", "agent_type": "coordinator:nope", "agent_model": "bad"},
        {"id": "C3", "agent_type": "coordinator:em"},
    ]
    found = _classes(rows, defs={})
    assert [f["detail"].split(":")[0] for f in found] == ["row C3"]


def test_row_without_agent_type_ignored():
    assert _classes([{"id": "C1"}, {"id": "C2", "agent_type": ""}]) == []


# --- emission through _extra_findings -------------------------------------------------------------


def test_extra_findings_maps_to_structural_at_class(monkeypatch, tmp_path):
    from coordinator_core.roadmap import prep_gate

    monkeypatch.setattr(prep_gate, "gate_plan", lambda *a, **k: {"terminal": False})
    monkeypatch.setattr(prep_gate, "plan_frontmatter", lambda text: {})
    monkeypatch.setattr(prep_gate, "raw_spine_rows", lambda text: [{"id": "C1", "agent_type": "coordinator:em"}])
    monkeypatch.setattr(psc, "micro_chunk_findings", lambda rows: [])
    monkeypatch.setattr(psc, "contributed_signal_findings", lambda *a, **k: [])
    monkeypatch.setattr(psc, "research_layout_findings", lambda *a, **k: [])
    monkeypatch.setattr(psc, "depends_on_plan_status_findings", lambda *a, **k: [])
    monkeypatch.setattr(psc, "abs_path_findings", lambda text: [])
    plan = tmp_path / "plan.md"
    plan.write_text("x", encoding="utf-8")
    found = psc._extra_findings(plan, "x")
    assert found == [
        {
            "row": "-",
            "error": "agent-type-undispatchable: row C1: agent_type 'coordinator:em' is never dispatchable",
            "at": "agent-type-undispatchable",
            "class": "structural",
        }
    ]


def test_agent_contract_findings_never_crashes_on_unresolvable_source(monkeypatch):
    from coordinator_core.hooks import block_unenumerated_agent_type as mod

    def boom(**kw):
        raise OSError("no plugin root")

    monkeypatch.setattr(mod, "scan_agent_definitions", boom)
    assert psc._agent_contract_findings([{"id": "C1", "agent_type": "coordinator:em"}]) == []
