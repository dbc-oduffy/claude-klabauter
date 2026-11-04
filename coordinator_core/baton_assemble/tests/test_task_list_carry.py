"""d1t `baton-stamp-task-list`: the handoff brief emits it and apply writes `## Open tasks`.

Drives `ba.brief("handoff", ...)` for the emitted directive and the registered
`_CLI_DISPATCH` handler for the write, against a tmp harness task store.
"""
from __future__ import annotations

import json

import pytest

import coordinator_core.baton_assemble as ba
import coordinator_core.baton_assemble.apply as ba_apply
from coordinator_core import task_carry
from coordinator_core.test_baton_assemble import _FAKE_OPERATOR_CONFIG, _init_repo, _write_artifact

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SID = "sid-task-carry"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("CLAUDE_SESSION_ID", SID)


def _seed_task(tmp_path, n, **fields):
    d = tmp_path / "claude-config" / "tasks" / SID
    d.mkdir(parents=True, exist_ok=True)
    data = {"id": str(n), "subject": f"s{n}", "description": f"d{n}", "status": "pending", "blockedBy": []}
    data.update(fields)
    (d / f"{n}.json").write_text(json.dumps(data), encoding="utf-8")


def _mint(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    decision = ba.brief("handoff", "", repo_root=repo).decision_object
    d1t = next((d for d in decision["directives"] if d["id"] == "d1t"), None)
    return repo, decision, d1t


def test_ac1_open_tasks_section_lists_open_tasks_in_id_order(tmp_path):
    _seed_task(tmp_path, 2, status="in_progress", blockedBy=["1", "3"])
    _seed_task(tmp_path, 1)
    _seed_task(tmp_path, 3, status="completed")
    repo, decision, d1t = _mint(tmp_path)
    assert d1t is not None
    assert d1t["cli"] == "baton-stamp-task-list"
    assert d1t["depends_on"] == ["d1"]
    assert d1t["args"][2:] == ["--session-id", SID]
    d2 = next(d for d in decision["directives"] if d["id"] == "d2")
    assert "d1t" in d2["depends_on"]

    out = d1t["args"][1]
    _write_artifact(repo / out, ["kind: session-handoff"])
    result = ba_apply._resolve_cli("baton-stamp-task-list")(d1t["args"], repo)
    assert result["stamped"] is True and result["count"] == 2

    text = (repo / out).read_text(encoding="utf-8")
    assert text.count("## Open tasks") == 1
    tasks = task_carry.parse_tasks_block(text)
    assert [t["id"] for t in tasks] == ["1", "2"]
    assert tasks[1] == {
        "id": "2", "subject": "s2", "description": "d2", "status": "in_progress", "blockedBy": ["1"],
    }


def test_ac2_empty_store_leaves_baton_byte_identical(tmp_path):
    repo, _decision, d1t = _mint(tmp_path)
    assert d1t is not None
    out = d1t["args"][1]
    _write_artifact(repo / out, ["kind: session-handoff"])
    before = (repo / out).read_bytes()
    result = ba_apply._resolve_cli("baton-stamp-task-list")(d1t["args"], repo)
    assert result["stamped"] is False and result["count"] == 0
    assert (repo / out).read_bytes() == before


def test_ac3_rerun_is_idempotent_and_changed_list_replaces_section(tmp_path):
    _seed_task(tmp_path, 1)
    repo, _decision, d1t = _mint(tmp_path)
    out = d1t["args"][1]
    _write_artifact(repo / out, ["kind: session-handoff"])
    dispatch = ba_apply._resolve_cli("baton-stamp-task-list")
    assert dispatch(d1t["args"], repo)["stamped"] is True
    after = (repo / out).read_bytes()
    assert dispatch(d1t["args"], repo)["stamped"] is False
    assert (repo / out).read_bytes() == after

    _seed_task(tmp_path, 2)
    assert dispatch(d1t["args"], repo)["stamped"] is True
    text = (repo / out).read_text(encoding="utf-8")
    assert text.count("## Open tasks") == 1
    assert [t["id"] for t in task_carry.parse_tasks_block(text)] == ["1", "2"]


def test_no_session_id_emits_no_directive(tmp_path, monkeypatch):
    for key in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(ba, "_resolve_current_session_id", lambda: None)
    repo = tmp_path / "repo"
    _init_repo(repo)
    lineage = ba.resolve_lineage("handoff", "", repo)
    directives = ba._build_directives("handoff", lineage, root=repo)
    assert all(d["id"] != "d1t" for d in directives)
    d2 = next(d for d in directives if d["id"] == "d2")
    assert "d1t" not in d2["depends_on"]
