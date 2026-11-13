"""tree_write_fence: both sides of each refuse/admit decision and the refusal record."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import tree_write_fence as fence
from coordinator_core.session import declared_writes


def test_path_outside_owned_refused():
    assert fence.check_tree_write("a.md", owned_paths={"b.md"}, base_blob="x", head_blob="x") == "outside-own-paths"


def test_no_owned_paths_refuses_everything():
    assert fence.check_tree_write("a.md", owned_paths=None, base_blob="x", head_blob="x") == "outside-own-paths"
    assert fence.check_tree_write("a.md", owned_paths=[], base_blob="x", head_blob="x") == "outside-own-paths"


def test_base_differs_from_head_refused():
    assert fence.check_tree_write("a.md", owned_paths={"a.md"}, base_blob="old", head_blob="new") == "older-than-head"


def test_base_vs_absent_head_refused():
    assert fence.check_tree_write("a.md", owned_paths={"a.md"}, base_blob="old", head_blob=None) == "older-than-head"


def test_own_path_base_equals_head_admitted():
    assert fence.check_tree_write("a.md", owned_paths={"a.md"}, base_blob="x", head_blob="x") is None


def test_own_path_absent_at_both_admitted():
    assert fence.check_tree_write("a.md", owned_paths={"a.md"}, base_blob=None, head_blob=None) is None


def test_backslash_and_dot_prefix_normalised():
    assert fence.check_tree_write("./d\\a.md", owned_paths={"d/a.md"}, base_blob="x", head_blob="x") is None


def test_record_refusal_appends_and_declares(tmp_path):
    with declared_writes.collecting() as got:
        p = fence.record_refusal(tmp_path / "rec", "a.md", "older-than-head", "run-1")
        fence.record_refusal(tmp_path / "rec", "b.md", "outside-own-paths", "run-1")
    assert p == tmp_path / "rec" / "tree-write-refusals.json"
    entries = json.loads(p.read_text(encoding="utf-8"))
    assert [(e["path"], e["reason"], e["run_id"]) for e in entries] == [
        ("a.md", "older-than-head", "run-1"),
        ("b.md", "outside-own-paths", "run-1"),
    ]
    assert str(p) in [str(x) for x in got]


def test_corrupt_record_moved_aside(tmp_path):
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "tree-write-refusals.json").write_text("{not json", encoding="utf-8")
    p = fence.record_refusal(rec, "a.md", "older-than-head", None)
    assert len(json.loads(p.read_text(encoding="utf-8"))) == 1
    assert (rec / "tree-write-refusals.json.corrupt").read_text(encoding="utf-8") == "{not json"
