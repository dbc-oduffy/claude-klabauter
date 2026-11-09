"""The RPC fire handle persists a peer target and reports driver exit once."""
from __future__ import annotations

from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest

from coordinator_core.agent_pipe import bridge
from coordinator_core.ops.workflow_fire import fire, op


@pytest.fixture
def registry(tmp_path, monkeypatch):
    from coordinator_core.ops.workflow_fire import authority
    monkeypatch.setattr(authority, "caller_principal", lambda *a: authority.Principal("verified-test-em", "em", {}))
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


def test_concurrent_terminal_refreshes_route_only_once(registry, monkeypatch):
    delivered = []
    monkeypatch.setattr(bridge, "route", lambda msg: delivered.append(msg) or SimpleNamespace(status="queued"))
    op._workflow_fire({"script_path": "run.mjs", "notify_to": "codex-peer"}, registry)
    monkeypatch.setattr(fire, "_refresh_record_state", lambda rec: {**rec, "state": "exited"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: op._workflow_fire_status({"fire_id": "f1"}, registry), range(2)))
    final = op._workflow_fire_status({"fire_id": "f1"}, registry)
    assert final["notified_at"] and len(delivered) == 1


def test_registry_sweep_also_delivers_terminal_notification(registry, monkeypatch):
    delivered = []
    monkeypatch.setattr(bridge, "route", lambda msg: delivered.append(msg) or SimpleNamespace(status="queued"))
    op._workflow_fire({"script_path": "run.mjs", "notify_to": "codex-peer"}, registry)
    monkeypatch.setattr(fire, "_refresh_record_state", lambda rec: {**rec, "state": "exited"})
    assert fire.count_live_fires(str(registry)) == 0
    assert len(delivered) == 1
    assert fire.count_live_fires(str(registry)) == 0 and len(delivered) == 1


def test_retry_uses_a_stable_message_identity(registry, monkeypatch):
    messages = []
    monkeypatch.setattr(bridge, "route", lambda msg: messages.append(msg) or SimpleNamespace(status="unroutable", reason="offline"))
    op._workflow_fire({"script_path": "run.mjs", "notify_to": "codex-peer"}, registry)
    monkeypatch.setattr(fire, "_refresh_record_state", lambda rec: {**rec, "state": "exited"})
    op._workflow_fire_status({"fire_id": "f1"}, registry)
    op._workflow_fire_status({"fire_id": "f1"}, registry)
    assert len(messages) == 2 and messages[0].id == messages[1].id


def test_late_notification_target_preserves_newer_terminal_state(registry):
    path = fire._record_path(registry, "f1")
    fire._write_record(path, {"fire_id": "f1", "state": "exited", "exit_code": 7})
    saved = op._store_notification_record(
        {"fire_id": "f1", "state": "pending", "repo_root": str(registry), "notify_to": "peer"}, registry)
    assert saved["state"] == "exited" and saved["exit_code"] == 7
    assert saved["notify_to"] == "peer"


def test_ambiguous_reservation_notice_does_not_claim_driver_completion(registry, monkeypatch):
    from coordinator_core.ops.workflow_fire import notify
    texts = []
    monkeypatch.setattr(bridge, "route", lambda msg: texts.append(msg.content) or SimpleNamespace(status="queued"))
    notify.notify_terminal({"fire_id": "lost", "state": "exited", "notify_to": "peer",
                            "execution_started": None, "execution_note": "Inspect before refiring."})
    assert "execution-unknown" in texts[0]
    assert "driver finished" not in texts[0]
