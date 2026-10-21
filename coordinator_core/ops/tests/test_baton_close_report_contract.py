"""Pins the baton.close_report contract: op key, enum members, record and result keys."""

from __future__ import annotations

import ast
from pathlib import Path

from coordinator_core.ops import baton_close_report_contract as c


def test_op_key_and_version():
    assert c.OP_KEY == "baton.close_report"
    assert c.SCHEMA_VERSION == 1


def test_evidence_class_members():
    assert {m.value for m in c.EvidenceClass} == {
        "plan", "sizing", "roadmap", "goal", "successor", "run",
    }


def test_run_constants_and_conflict_note():
    assert (c.RUN_FIELD_BATON, c.RUN_FIELD_SIZING, c.RUN_FIELD_CLAIMED) == (
        "run.baton", "run.sizing_rel", "run.claimed_batons",
    )
    assert (c.RUN_COMMITTED, c.RUN_ENDED, c.RUN_LIVE) == ("committed", "ended", "live")
    assert c.CONFLICT_WINNERS == ("done", "contradicting")
    assert set(c.ConflictNote.__annotations__) == {"done", "contradicting", "winner", "rule"}


def test_residue_reason_members():
    assert {m.value for m in c.ResidueReason} == {
        "target_unreadable", "target_unstatused", "target_missing", "live_successor",
        "claimed", "evidence_conflict", "unreadable_frontmatter",
        "spinoff_kind", "no_deliverable_id",
    }


def test_verdict_members():
    assert {m.value for m in c.Verdict} == {"closable", "residue", "no_evidence"}


def test_evidence_keys():
    assert set(c.Evidence.__annotations__) == set(c.EVIDENCE_KEYS)


def test_header_keys():
    assert set(c.BatonHeader.__annotations__) == set(c.HEADER_KEYS)


def test_header_keys_pinned_order_and_reverse_fields():
    assert c.HEADER_KEYS == (
        "path", "status", "predecessor", "governing_plan", "origin_plan_id",
        "sizing_object", "roadmap_id", "origin_goal_id", "deliverable_id", "kind",
    )
    assert c.REVERSE_FIELD_DELIVERABLE == "plan.deliverable_id"
    assert c.REVERSE_FIELD_PREDECESSOR == "plan.predecessor_handoff"


def test_result_keys():
    assert set(c.CloseReport.__annotations__) == set(c.REPORT_KEYS)
    assert set(c.ClosableItem.__annotations__) == {"handoff_path", "evidence", "conflict"}
    assert set(c.ResidueItem.__annotations__) == {
        "handoff_path", "reason", "evidence", "conflict",
    }


def test_module_has_no_io_imports():
    tree = ast.parse(Path(c.__file__).read_text(encoding="utf-8"))
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            names.add(n.module.split(".")[0])
    assert not names & {"yaml", "subprocess", "os", "pathlib", "json"}
