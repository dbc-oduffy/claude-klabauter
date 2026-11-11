"""checkpoint_reds: classify, mint, idempotence, listing and the op handler."""

from __future__ import annotations

import yaml

from coordinator_core.ipc import get_op_handler
from coordinator_core.ops.dispatch_emit import checkpoint_reds as cr


def test_classify_splits_base_red_from_introduced():
    out = cr.classify(["a", "b"], base_red={"a"})
    assert out == [{"id": "a", "class": "red_at_base"}, {"id": "b", "class": "introduced"}]


def test_classify_normalises_backslashes_and_dedups():
    out = cr.classify(["t\\x.py::a", "t/x.py::a"], base_red={"t/x.py::a"})
    assert out == [{"id": "t/x.py::a", "class": "red_at_base"}]


def test_mint_writes_schema_valid_open_row_and_reuses_it(tmp_path):
    rows = cr.mint_fix_rows(tmp_path, "run-1", ["t/x.py::b"])
    assert len(rows) == 1
    data = yaml.safe_load((tmp_path / rows[0]["path"]).read_text(encoding="utf-8"))
    assert data["status"] == "open" and data["introduced_red"] is True
    assert data["test_id"] == "t/x.py::b" and data["run_id"] == "run-1"
    for key in ("created", "title", "body", "surface", "severity"):
        assert data[key]
    assert rows[0]["path"].endswith(f"-{rows[0]['id']}.yaml")
    again = cr.mint_fix_rows(tmp_path, "run-2", ["t/x.py::b"])
    assert again == rows
    assert len(list((tmp_path / cr.BUG_HOME).iterdir())) == 1


def test_open_rows_list_until_closed(tmp_path):
    rows = cr.mint_fix_rows(tmp_path, "run-1", ["t/x.py::b"])
    assert [r["test_id"] for r in cr.open_introduced_fix_rows(tmp_path)] == ["t/x.py::b"]
    path = tmp_path / rows[0]["path"]
    path.write_text(path.read_text(encoding="utf-8").replace("status: open", "status: closed"), encoding="utf-8")
    assert cr.open_introduced_fix_rows(tmp_path) == []
    fresh = cr.mint_fix_rows(tmp_path, "run-3", ["t/x.py::b"])
    assert fresh[0]["path"] != rows[0]["path"]


def test_non_introduced_rows_are_not_listed(tmp_path):
    home = tmp_path / cr.BUG_HOME
    home.mkdir(parents=True)
    (home / "2026-01-01-other-aaaaaaaaaaaa.yaml").write_text("status: open\n", encoding="utf-8")
    assert cr.open_introduced_fix_rows(tmp_path) == []


def test_commit_lines_lists_each_red_and_fix_rows():
    block = cr.commit_lines(cr.classify(["a", "b"], {"a"}), ["abc123abc123"])
    assert "red: a (red_at_base)" in block and "red: b (introduced)" in block and "abc123abc123" in block
    assert cr.commit_lines([], []) == ""


def test_handler_end_to_end_mints_only_introduced(tmp_path):
    handler = get_op_handler(cr.OP_NAME)
    reply = handler({"run_id": "r", "failing": ["a", "b"], "base_red": ["a"]}, repo_root=tmp_path)
    assert reply["ok"] and len(reply["fix_ids"]) == 1 and len(reply["fix_paths"]) == 1
    assert (tmp_path / reply["fix_paths"][0]).is_file()
    assert "red: a (red_at_base)" in reply["commit_block"]
    clean = handler({"run_id": "r", "failing": ["a"], "base_red": ["a"]}, repo_root=tmp_path)
    assert clean["ok"] and clean["fix_ids"] == []


def test_handler_refuses_missing_run_id(tmp_path):
    assert get_op_handler(cr.OP_NAME)({"failing": ["a"]}, repo_root=tmp_path)["ok"] is False
