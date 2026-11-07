"""The RPC fire handle persists a peer target and reports driver exit once."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from coordinator_core.agent_pipe import bridge
from coordinator_core.ops.workflow_fire import fire, op


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(fire, "_registry_dir", lambda cwd=None: tmp_path)
    record = {"fire_id": "f1", "repo_root": str(tmp_path), "state": "running", "exit_code": None}
    monkeypatch.setattr(fire, "fire_workflow", lambda *a, **kw: dict(record))
    return tmp_path


def test_rpc_fire_to_terminal_notification_is_persisted_once(registry, monkeypatch):
    delivered = []
    monkeypatch.setattr(bridge, "route", lambda msg: delivered.append(msg) or SimpleNamespace(status="delivered"))
    record = op._workflow_fire({"script_path": "run.mjs", "notify_to": "codex-peer"}, registry)
    assert record["notify_to"] == "codex-peer"
    assert delivered == []
    monkeypatch.setattr(fire, "_refresh_record_state", lambda rec: {**rec, "state": "exited"})
    terminal = op._workflow_fire_status({"fire_id": "f1"}, registry)
    assert terminal["notified_at"] and terminal["notify_status"] == "delivered"
    assert delivered[0].to_identity == "codex-peer"
    assert "driver-done, not run-completed" in delivered[0].content
    assert op._workflow_fire_status({"fire_id": "f1"}, registry)["notified_at"] == terminal["notified_at"]
    assert len(delivered) == 1


def test_running_status_and_missing_record_never_notify(registry, monkeypatch):
    monkeypatch.setattr(bridge, "route", lambda msg: pytest.fail("premature notification"))
    op._workflow_fire({"script_path": "run.mjs", "notify_to": "codex-peer"}, registry)
    monkeypatch.setattr(fire, "_refresh_record_state", lambda rec: rec)
    assert op._workflow_fire_status({"fire_id": "f1"}, registry)["state"] == "running"
    assert op._workflow_fire_status({"fire_id": "absent"}, registry) == {"fire_id": "absent", "found": False}


def test_unroutable_peer_is_retryable_on_later_status(registry, monkeypatch):
    op._workflow_fire({"script_path": "run.mjs", "notify_to": "codex-peer"}, registry)
    monkeypatch.setattr(fire, "_refresh_record_state", lambda rec: {**rec, "state": "exited"})
    monkeypatch.setattr(bridge, "route", lambda msg: SimpleNamespace(status="unroutable", reason="offline"))
    failed = op._workflow_fire_status({"fire_id": "f1"}, registry)
    assert "notified_at" not in failed and "offline" in failed["notify_error"]
    monkeypatch.setattr(bridge, "route", lambda msg: SimpleNamespace(status="handed-off"))
    retried = op._workflow_fire_status({"fire_id": "f1"}, registry)
    assert retried["notify_status"] == "handed-off" and retried["notified_at"]
    assert "notify_error" not in retried


@pytest.mark.parametrize("target", ["", " ", 7, []])
def test_invalid_target_cannot_spawn(registry, monkeypatch, target):
    monkeypatch.setattr(fire, "fire_workflow", lambda *a, **kw: pytest.fail("spawned invalid fire"))
    with pytest.raises(ValueError, match="notify_to"):
        op._workflow_fire({"script_path": "run.mjs", "notify_to": target}, registry)


def test_notification_write_failure_keeps_the_spawned_handle(registry, monkeypatch):
    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(op, "_store_notification_record", fail)
    result = op._workflow_fire({"script_path": "run.mjs", "notify_to": "codex-peer"}, registry)
    assert result["fire_id"] == "f1" and "disk full" in result["notify_error"]
