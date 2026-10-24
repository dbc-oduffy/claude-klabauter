"""A peer-repo run whose register row only mentions the engine stays at its own root."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import engine_owned_rows
from coordinator_core.ops.dispatch_emit.engine_owned_rows import engine_owner_rows

MENTION = [{"id": "R1", "source_text": "see memo about claude-klabauter coordinator_core", "met_by": "klabauter memo"}]


@pytest.fixture(autouse=True)
def _resolver(monkeypatch):
    monkeypatch.setattr(engine_owned_rows, "resolve_engine_root", lambda: ("engine-stub", "resolved-engine"))


def test_mention_only_register_row_in_peer_run_is_cwd_owned(tmp_path):
    peer_plan = [{"id": "C1", "writes": ["addon/Source/x.cpp"]}]
    assert engine_owner_rows(peer_plan, MENTION, "", tmp_path) == []


def test_engine_plan_row_keeps_engine_routing(tmp_path):
    plan = [{"id": "C1", "writes": ["coordinator_core/x.py"]}]
    assert [r["id"] for r in engine_owner_rows(plan, MENTION, "", tmp_path)] == ["C1", "R1"]


def test_engine_owner_gate_keeps_mention_routing_with_peer_writes(tmp_path):
    plan = [{"id": "C1", "writes": ["addon/x.cpp"], "external_gate": [{"owner_repo": "claude-klabauter"}]}]
    assert [r["id"] for r in engine_owner_rows(plan, MENTION, "", tmp_path)] == ["C1", "R1"]


def test_engine_evidence_keeps_engine_routing(tmp_path):
    plan = [{"id": "C1", "writes": ["addon/x.cpp"]}]
    evidence = "rows:\n  C1:\n  - text: delivered in coordinator_core/ops/x.py\n"
    assert [r["id"] for r in engine_owner_rows(plan, MENTION, evidence, tmp_path)] == ["C1", "R1"]
