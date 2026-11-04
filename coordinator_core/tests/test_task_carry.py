"""Tests for coordinator_core.task_carry (tmp_path stores only)."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core import task_carry


def _write(store: Path, sid: str, n: int, **fields) -> None:
    d = store / sid
    d.mkdir(parents=True, exist_ok=True)
    rec = {"id": str(n), "subject": f"s{n}", "description": f"d{n}", "status": "pending",
           "blocks": [], "blockedBy": []}
    rec.update(fields)
    (d / f"{n}.json").write_text(json.dumps(rec), encoding="utf-8")


def test_completed_excluded_numeric_order_and_edges_filtered(tmp_path):
    _write(tmp_path, "S", 10, blockedBy=["2", "3"])
    _write(tmp_path, "S", 2, status="in_progress")
    _write(tmp_path, "S", 3, status="completed")
    (tmp_path / "S" / "9.json").write_text("{not json", encoding="utf-8")
    tasks = task_carry.read_open_tasks("S", tmp_path)
    assert [t["id"] for t in tasks] == ["2", "10"]
    assert tasks[1]["blockedBy"] == ["2"]
    assert set(tasks[0]) == {"id", "subject", "description", "status", "blockedBy"}


def test_empty_or_missing_dir(tmp_path):
    assert task_carry.read_open_tasks("nope", tmp_path) == []
    (tmp_path / "E").mkdir()
    assert task_carry.read_open_tasks("E", tmp_path) == []
    assert task_carry.read_open_tasks("", tmp_path) == []
    assert task_carry.read_open_tasks("../x", tmp_path) == []
    assert task_carry.render_tasks_section([]) == ""


def test_default_store_root_uses_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write(tmp_path / "tasks", "S", 1)
    assert [t["id"] for t in task_carry.read_open_tasks("S")] == ["1"]


def test_render_parse_round_trip(tmp_path):
    _write(tmp_path, "S", 1, description="line: with colon\nand 'quotes' # hash")
    _write(tmp_path, "S", 2, status="in_progress", blockedBy=["1"])
    tasks = task_carry.read_open_tasks("S", tmp_path)
    text = "intro\n\n" + task_carry.render_tasks_section(tasks) + "\ntail\n"
    assert text.count("## Open tasks") == 1
    assert "```yaml tasks" in text
    assert task_carry.parse_tasks_block(text) == tasks


def test_parse_absent_or_malformed_returns_empty():
    assert task_carry.parse_tasks_block("") == []
    assert task_carry.parse_tasks_block("no block here") == []
    assert task_carry.parse_tasks_block("```yaml tasks\n: : [\n```\n") == []
    assert task_carry.parse_tasks_block("```yaml tasks\nfoo: bar\n```\n") == []
    assert task_carry.parse_tasks_block("```yaml\n- id: '1'\n```\n") == []


def test_recreate_directives_order_and_shape():
    tasks = [
        {"id": "1", "subject": "a", "description": "da", "status": "pending", "blockedBy": []},
        {"id": "2", "subject": "b", "description": "db", "status": "in_progress", "blockedBy": ["1"]},
        {"id": "3", "subject": "c", "description": "dc", "status": "pending", "blockedBy": ["1", "2"]},
    ]
    steps = task_carry.recreate_directives(tasks)
    assert [s["tool"] for s in steps] == ["TaskCreate"] * 3 + ["TaskUpdate"] * 2
    assert all(s["advisory"] is True for s in steps)
    assert steps[0]["args"] == {"subject": "a", "description": "da"}
    assert steps[3] == {"tool": "TaskUpdate", "old_id": "2",
                        "args": {"status": "in_progress", "addBlockedBy": ["1"]}, "advisory": True}
    assert steps[4]["args"] == {"addBlockedBy": ["1", "2"]}
    assert task_carry.recreate_directives([]) == []
