from __future__ import annotations

import json
import os

import pytest

from coordinator_core.warm import telemetry


@pytest.fixture(autouse=True)
def _clean_ring():
    telemetry._REQUESTS.clear()
    yield
    telemetry._REQUESTS.clear()


def test_record_is_memory_only_until_flush(tmp_path):
    telemetry.record_request(op="guard.check", session="s1", enqueue_ts=1.0, start_ts=2.0, end_ts=3.0, busy_workers=4)
    assert telemetry.request_samples() == [
        {"op": "guard.check", "session": "s1", "enqueue_ts": 1.0, "start_ts": 2.0, "end_ts": 3.0, "busy_workers": 4}
    ]
    assert not telemetry.request_telemetry_path(tmp_path).exists()


def test_ring_is_bounded_and_evicts_oldest():
    for i in range(telemetry.REQUEST_TELEMETRY_KEEP + 5):
        telemetry.record_request(op=f"op{i}")
    rows = telemetry.request_samples()
    assert len(rows) == telemetry.REQUEST_TELEMETRY_KEEP
    assert rows[0]["op"] == "op5"


def test_flush_appends_once_and_drains(tmp_path):
    telemetry.record_request(op="a")
    telemetry.record_request(op="b")
    telemetry.flush_requests(tmp_path)
    lines = telemetry.request_telemetry_path(tmp_path).read_text().splitlines()
    assert [json.loads(x)["op"] for x in lines] == ["a", "b"]
    assert telemetry.request_samples() == []
    telemetry.flush_requests(tmp_path)
    assert len(telemetry.request_telemetry_path(tmp_path).read_text().splitlines()) == 2


def test_record_and_flush_never_raise(tmp_path, monkeypatch):
    telemetry.record_request(op="a")

    def _boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(telemetry.locked_write, "held_lock", _boom)
    telemetry.flush_requests(tmp_path)
    monkeypatch.setattr(telemetry, "_REQUESTS", object())
    telemetry.record_request(op="x")
    monkeypatch.undo()


def test_server_boot_names_parent_and_reason(tmp_path):
    telemetry.record_server_boot(listener_secs=0.1, ready_secs=0.2, pid=7, engine_root=tmp_path, boot_reason="supervisor-respawn")
    row = telemetry.server_boot_samples(tmp_path)[0]
    assert row["parent_pid"] == os.getppid()
    assert row["boot_reason"] == "supervisor-respawn"


def test_server_boot_reason_omitted_when_unnamed(tmp_path):
    telemetry.record_server_boot(listener_secs=0.1, ready_secs=0.2, pid=7, engine_root=tmp_path, parent_pid=99)
    row = telemetry.server_boot_samples(tmp_path)[0]
    assert row["parent_pid"] == 99
    assert "boot_reason" not in row
