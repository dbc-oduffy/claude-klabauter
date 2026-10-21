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
        "plan", "sizing", "roadmap", "goal", "successor",
    }


def test_residue_reason_members():
    assert {m.value for m in c.ResidueReason} == {
        "target_unreadable", "target_missing", "live_successor",
        "claimed", "evidence_conflict", "unreadable_frontmatter",
    }


def test_verdict_members():
    assert {m.value for m in c.Verdict} == {"closable", "residue", "no_evidence"}


def test_evidence_keys():
    assert set(c.Evidence.__annotations__) == set(c.EVIDENCE_KEYS)


def test_header_keys():
    assert set(c.BatonHeader.__annotations__) == set(c.HEADER_KEYS)


def test_result_keys():
    assert set(c.CloseReport.__annotations__) == set(c.REPORT_KEYS)
    assert set(c.ClosableItem.__annotations__) == {"handoff_path", "evidence"}
    assert set(c.ResidueItem.__annotations__) == {"handoff_path", "reason", "evidence"}


def test_module_has_no_io_imports():
    tree = ast.parse(Path(c.__file__).read_text(encoding="utf-8"))
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            names.add(n.module.split(".")[0])
    assert not names & {"yaml", "subprocess", "os", "pathlib", "json"}
