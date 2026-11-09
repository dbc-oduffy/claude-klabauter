"""Pure unit coverage for headless truth on the existing 70 notification path."""
from concurrent.futures import ThreadPoolExecutor
import os
import threading
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import psutil
import pytest

from coordinator_core.ops.workflow_fire import fire, headless, notify


def record(**extra):
    return {"executor": "headless", "fire_id": "fixture", "state": "exited", "pid": 0,
            "exit_code": 0, "outcome": "clean", "result": {"actual": "returned"},
            "landing": {"sha": "actual-sha"}, "journal_path": "actual-journal",
            "notify_to": "codex-parent", **extra}


@pytest.fixture(autouse=True)
def preserve_headless_semantics(monkeypatch):
    monkeypatch.setattr(fire, "_refresh_record_state", lambda *a: pytest.fail("Claude log classification"))
    monkeypatch.setattr(fire, "_write_record", lambda *a: pytest.fail("Claude annotation on write"))


def test_live_writer_defers_shared_delivery_without_record_changes(tmp_path, monkeypatch):
    path = tmp_path / "fire.json"
    value = record(pid=os.getpid(), pid_birth=psutil.Process().create_time())
    headless._save(path, value)
    before = path.read_bytes()
    monkeypatch.setattr(notify.bridge, "route", lambda *a: pytest.fail("notified before owner release"))
    assert notify.refresh_record(path, value) == value
    assert path.read_bytes() == before


def test_released_writer_uses_existing_stable_id_and_preserves_landing(tmp_path, monkeypatch):
    path = tmp_path / "fire.json"
    value = record(notify_owner_pid=os.getpid(), notify_owner_released=True)
    headless._save(path, value)
    delivered = []
    monkeypatch.setattr(notify.bridge, "route", lambda msg: delivered.append(msg) or SimpleNamespace(status="queued"))
    result = notify.refresh_record(path, value)
    assert result["notified_at"] and result["result"] == value["result"] and result["landing"] == value["landing"]
    assert delivered[0].id == uuid5(NAMESPACE_URL, "workflow.fire:fixture:codex-parent").hex
    assert "actual-sha" in delivered[0].content and "actual-journal" in delivered[0].content
    assert "driver-done" not in delivered[0].content
    notify.refresh_record(path, result)
    assert len(delivered) == 1


def test_post_owner_read_keeps_final_checkpoint_written_during_death_probe(tmp_path, monkeypatch):
    path = tmp_path / "fire.json"
    stale = record(state="running", pid=123)
    final = record(notified_at="accepted", notify_status="queued", landing={"sha": "final-sha"})
    headless._save(path, stale)
    def completed(pid):
        headless._save(path, final)
        raise psutil.NoSuchProcess(pid)
    monkeypatch.setattr(psutil, "Process", completed)
    monkeypatch.setattr(notify.bridge, "route", lambda *a: pytest.fail("replayed completed writer"))
    assert notify.refresh_record(path, stale) == final
    assert fire._read_record(path) == final


def test_dead_before_terminal_write_reports_incomplete_without_claude_envelope(tmp_path, monkeypatch):
    path = tmp_path / "fire.json"
    value = record(state="running")
    headless._save(path, value)
    delivered = []
    monkeypatch.setattr(notify.bridge, "route", lambda msg: delivered.append(msg) or SimpleNamespace(status="queued"))
    result = notify.refresh_record(path, value)
    assert result["outcome"] == "unknown" and result["exit_code"] is None
    assert result["result"] == value["result"] and result["landing"] == value["landing"]
    assert "Workflow incomplete" in delivered[0].content and "driver-done" not in delivered[0].content


def test_shared_kernel_claim_serializes_owner_and_recovery_actors(tmp_path, monkeypatch):
    path = tmp_path / "fire.json"
    value = record(notify_owner_released=True)
    headless._save(path, value)
    entered, release = threading.Event(), threading.Event()
    deliveries = []
    def deliver(message):
        deliveries.append(message)
        entered.set()
        assert release.wait(2)
        return SimpleNamespace(status="queued")
    monkeypatch.setattr(notify.bridge, "route", deliver)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(notify.refresh_record, path, value)
        try:
            assert entered.wait(2)
            assert not notify.refresh_record(path, value).get("notified_at")
            assert len(deliveries) == 1
        finally:
            release.set()
        assert first.result(timeout=2)["notified_at"]


def test_pid_reuse_rejects_old_birth_and_unreadable_identity_defers(monkeypatch):
    process = SimpleNamespace(create_time=lambda: 20, is_running=lambda: True, status=lambda: "running")
    monkeypatch.setattr(psutil, "Process", lambda pid: process)
    assert not notify._headless_owner_live(record(pid=123, pid_birth=10))
    assert notify._headless_owner_live(record(pid=123, pid_birth=20))
    def denied(pid):
        raise psutil.AccessDenied(pid)
    monkeypatch.setattr(psutil, "Process", denied)
    assert notify._headless_owner_live(record(pid=123))


def test_unaddressed_headless_history_avoids_legacy_fastpath_classification(tmp_path):
    path = tmp_path / "fire.json"
    value = record(notify_to=None)
    headless._save(path, value)
    assert notify.refresh_record(path, value) == value
