"""ask_gate research branch: a research sizing hands back at every t-shirt, research plus writes is refused."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.ask_contract import RESEARCH_MIXED_REFUSAL
from coordinator_core.ops._research_sizing import drop_research_block
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.warp_fire import fire_command

REL = "state/sizings/2026-10-16-research-gate.yaml"
RESEARCH = {"value_class": "scouts", "appetite": "small", "sources": ["web"], "questions": ["why?"]}
ROUTE = {"XS": "dispatch", "S": "spec-dispatch", "M": "plan", "L": "spec-dispatch", "XL": "plan"}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    return tmp_path


def _put(repo, tshirt, research=None):
    doc = {
        "schema": "sizing-object",
        "name": "research fixture",
        "intent": "answer a question",
        "estimate": {"tshirt": tshirt, "provisional": True},
        "route": ROUTE[tshirt],
        "detents": [],
        "fork": None,
        "xl_exit": None,
        "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-fixture-abc123",
        "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": None},
    }
    if research is not None:
        doc["research"] = research
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


@pytest.mark.parametrize("tshirt", ["XS", "S", "M", "L", "XL"])
def test_research_sizing_hands_back_at_every_tshirt(repo, tshirt):
    _put(repo, tshirt, RESEARCH)
    v = gate(repo, REL)
    assert v.arm is None and v.baton is None
    assert v.halt["kind"] == "research" and v.halt["route"] == "research"
    assert v.halt["fire"] == [fire_command(REL)]
    assert REL in v.halt["reason"] and "research door" not in v.halt["reason"]
    assert "/coordinator:research" not in v.halt["reason"]
    assert "engine-ask" not in v.halt["reason"]


@pytest.mark.parametrize("tshirt", ["XS", "M"])
def test_research_plus_writes_is_refused_with_two_step_remedy(repo, tshirt):
    _put(repo, tshirt, RESEARCH)
    v = gate(repo, REL, writes=["a.py"])
    assert v.arm is None and v.halt["kind"] == "refusal"
    assert v.halt["reason"] == RESEARCH_MIXED_REFUSAL
    assert "/coordinator:research" in RESEARCH_MIXED_REFUSAL
    assert "engine-ask" not in RESEARCH_MIXED_REFUSAL
    assert "research.close" in RESEARCH_MIXED_REFUSAL
    assert "workflow.revise build_only=true" in RESEARCH_MIXED_REFUSAL


@pytest.mark.parametrize("tshirt", ["XS", "S"])
def test_dropped_research_block_arms_with_writes(repo, tshirt):
    _put(repo, tshirt, RESEARCH)
    assert drop_research_block(repo, REL) is not None
    v = gate(repo, REL, writes=["a.py"])
    assert v.halt is None
    assert v.arm is not None


def test_pure_research_with_empty_writes_is_not_mixed(repo):
    _put(repo, "S", RESEARCH)
    assert gate(repo, REL, writes=[]).halt["kind"] == "research"


@pytest.mark.parametrize("research", [None, {}])
def test_non_research_sizing_keeps_its_arm(repo, research):
    _put(repo, "S", research)
    v = gate(repo, REL, writes=["a.py"])
    assert v.arm == "s" and v.halt is None
