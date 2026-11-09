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
        {
            "op": "guard.check",
            "session": "s1",
            "enqueue_ts": 1.0,
            "start_ts": 2.0,
            "end_ts": 3.0,
            "busy_workers": 4,
            "queue_wait_ms": 1000.0,
        }
    ]
    assert not telemetry.request_telemetry_path(tmp_path).exists()


def test_record_request_keeps_every_named_field_and_is_keyword_only():
    telemetry.record_request(
        op="hooks.preuse_bash_dispatch", session="s2", enqueue_ts=1.0, start_ts=1.5, end_ts=2.0,
        busy_workers=30, outcome="ok", pid=11, worker_pid=12, lane="guard", dispatch_key="k1",
    )
    row = telemetry.request_samples()[0]
    assert row == {
        "op": "hooks.preuse_bash_dispatch", "session": "s2", "enqueue_ts": 1.0, "start_ts": 1.5, "end_ts": 2.0,
        "busy_workers": 30, "outcome": "ok", "pid": 11, "worker_pid": 12, "lane": "guard", "dispatch_key": "k1",
        "queue_wait_ms": 500.0,
    }
    with pytest.raises(TypeError):
        telemetry.record_request("positional")


def test_queue_wait_ms_omitted_without_both_timestamps():
    telemetry.record_request(op="a", enqueue_ts=1.0)
    telemetry.record_request(op="b", start_ts=2.0)
    assert all("queue_wait_ms" not in r for r in telemetry.request_samples())


def test_slow_request_rows_returns_only_rows_over_threshold():
    telemetry.record_request(op="slow", session="s9", enqueue_ts=100.0, start_ts=108.5, end_ts=109.0, busy_workers=6)
    telemetry.record_request(op="fast", session="s1", enqueue_ts=100.0, start_ts=100.01, end_ts=100.02, busy_workers=1)
    telemetry.record_request(op="exact", enqueue_ts=0.0, start_ts=1.0, end_ts=telemetry.SLOW_REQUEST_SECS)
    telemetry.record_request(op="untimed")
    assert telemetry.slow_request_rows(telemetry.request_samples()) == [
        {"op": "slow", "session": "s9", "queue_wait_ms": 8500.0, "busy_workers": 6}
    ]
    assert [r["op"] for r in telemetry.slow_request_rows(telemetry.request_samples(), threshold_secs=0.5)] == ["slow", "exact"]


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


def test_record_request_carries_slow_phase_and_spans_and_omits_none():
    telemetry.record_request(op="a", slow_phase="git_read", spans=[{"name": "git_read", "wall_ms": 1200.0}])
    telemetry.record_request(op="b")
    first, second = telemetry.request_samples()
    assert first["slow_phase"] == "git_read" and first["spans"] == [{"name": "git_read", "wall_ms": 1200.0}]
    assert "slow_phase" not in second and "spans" not in second


def test_chokepoint_forwards_slow_phase_for_preuse_ops_only(monkeypatch):
    import asyncio

    from coordinator_core import ipc
    from coordinator_core.telemetry import op_row_fields

    async def _handler(params, repo_root=None):
        op_row_fields.attach(slow_phase="x", spans=[["x", 9.0]])
        return {"ok": True}

    monkeypatch.setitem(ipc._REGISTRY, "hooks.preuse_c2_probe", _handler)
    monkeypatch.setitem(ipc._REGISTRY, "other.c2_probe", _handler)

    def _run(method):
        sink: dict = {}
        token = ipc.PREUSE_ROW_FIELDS_SINK.set(sink)
        try:
            asyncio.run(ipc.dispatch_message({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}))
        finally:
            ipc.PREUSE_ROW_FIELDS_SINK.reset(token)
        return sink

    assert _run("hooks.preuse_c2_probe") == {"slow_phase": "x", "spans": [["x", 9.0]]}
    assert _run("other.c2_probe") == {}


def test_pool_dispatch_moves_worker_fields_to_row_and_strips_reply():
    from coordinator_core.warm import server

    class _Fake:
        def _pool_dispatch_untimed(self, msg, *, caller=None, dispatch_key=None):
            return {"jsonrpc": "2.0", "id": 1, "result": {}, server._ROW_FIELDS_KEY: {"slow_phase": "x", "spans": [["x", 5.0]]}}

    server._REQUEST_ROW.value = pending = {}
    try:
        reply = server._ServerContext._pool_dispatch(_Fake(), {"method": "hooks.preuse_bash_dispatch", "id": 1})
    finally:
        server._REQUEST_ROW.value = None
    assert server._ROW_FIELDS_KEY not in reply
    assert pending["slow_phase"] == "x" and pending["spans"] == [["x", 5.0]]
    server._ServerContext._record_served_row(_Fake(), pending, "pool", 1.0, 2.0, 0)
    row = telemetry.request_samples()[-1]
    assert row["slow_phase"] == "x" and row["spans"] == [["x", 5.0]]
