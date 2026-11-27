"""Pins the baton.bulk_set contract: op key, enum members, key tuples vs TypedDicts, no I/O imports."""

from __future__ import annotations

import ast
from pathlib import Path

from coordinator_core.ops import baton_bulk_set_contract as c


def test_op_key_and_version():
    assert c.OP_KEY == "baton.bulk_set"
    assert c.SCHEMA_VERSION == 1


def test_refusal_reason_members():
    assert {m.value for m in c.RefusalReason} == {
        "not_found", "unreadable_frontmatter", "bad_eta", "bad_owner", "close_without_evidence",
        "bad_evidence", "missing_deliverable_id", "write_failed",
    }


def test_close_verdict_admits_only_shipped():
    assert c.CLOSE_VERDICTS == ("shipped",)


def test_key_tuples_match_typeddicts():
    assert set(c.RESULT_KEYS) == set(c.BulkSetResult.__annotations__)
    assert set(c.ITEM_KEYS) == set(c.Item.__annotations__)
    assert set(c.CLOSE_KEYS) == set(c.Close.__annotations__)
    assert set(c.APPLIED_KEYS) == set(c.AppliedItem.__annotations__)
    assert set(c.UNCHANGED_KEYS) == set(c.UnchangedItem.__annotations__)
    assert set(c.REFUSED_KEYS) == set(c.RefusedItem.__annotations__)


def test_close_is_the_only_optional_item_key():
    assert c.Item.__required_keys__ == {"path", "owner_session", "eta"}
    assert c.Item.__optional_keys__ == {"close"}


def test_reply_carries_the_pinned_keys():
    assert {"applied", "unchanged", "refused", "dry_run", "commit_sha", "pathspec"} <= set(c.RESULT_KEYS)


def test_module_imports_no_io():
    tree = ast.parse(Path(c.__file__).read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
    assert mods <= {"enum", "typing"}
