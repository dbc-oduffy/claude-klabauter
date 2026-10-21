"""Pins the baton.close_commit contract: op key, enum members, result keys, no I/O imports."""

from __future__ import annotations

import ast
from pathlib import Path

from coordinator_core.ops import baton_close_commit_contract as c
from coordinator_core.ops import baton_close_report_contract as r


def test_op_key_and_version():
    assert c.OP_KEY == "baton.close_commit"
    assert c.SCHEMA_VERSION == 1


def test_refusal_reason_members():
    assert {m.value for m in c.RefusalReason} == {
        "bad_row", "not_live", "unreadable_frontmatter", "claimed", "continued",
        "live_successor", "evidence_stale", "predicate_refused",
        "no_deliverable_id", "no_evidence_sha", "write_failed",
    }


def test_flip_request_carries_shipped_in():
    assert c.FLIP_REQUEST_KEYS == ("path", "deliverable_id", "shipped_in")


def test_result_keys_match_typeddict():
    assert set(c.RESULT_KEYS) == set(c.CommitResult.__annotations__)
    assert set(c.APPLIED_KEYS) == set(c.AppliedItem.__annotations__)
    assert set(c.REFUSED_KEYS) == set(c.RefusedItem.__annotations__)
    assert set(c.ALREADY_TERMINAL_KEYS) == set(c.AlreadyTerminalItem.__annotations__)
    assert set(c.FLIP_REQUEST_KEYS) == set(c.FlipRequest.__annotations__)


def test_result_keys_pinned():
    assert c.RESULT_KEYS == (
        "exit_code", "schema_version", "op", "rows_received", "applied",
        "already_terminal", "refused", "pathspec", "commit_subject",
    )
    assert "commit_sha" not in c.RESULT_KEYS
    assert "commit_error" not in c.RESULT_KEYS
    assert c.APPLIED_KEYS == ("handoff_path", "advanced_by", "advanced_at", "shipped_in")
    assert c.REFUSED_KEYS == ("handoff_path", "reason", "detail", "evidence")


def test_row_type_is_close_reports_own_object():
    assert c.CommitRow is r.ClosableItem


def test_module_imports_no_io():
    tree = ast.parse(Path(c.__file__).read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
    assert mods <= {
        "enum", "typing",
        "coordinator_core.ops.baton_close_report_contract",
    }
