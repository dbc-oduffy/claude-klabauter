
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from coordinator_core.warm import http_listener, telemetry


def _post(port: int, payload: dict, timeout: float = 5.0):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "http://%s:%d/hook" % (http_listener.bind_host(), port),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def test_record_degrade_writes_a_row_with_kind_and_cause(tmp_path):
    telemetry.record_degrade(
        kind=telemetry.KIND_COLD_RUN, cause="something ran cold", engine_root=tmp_path
    )

    rows = telemetry.degrade_samples(tmp_path)
    assert len(rows) == 1
    assert rows[0]["kind"] == telemetry.KIND_COLD_RUN
    assert rows[0]["cause"] == "something ran cold"
    assert "ts" in rows[0]


def test_record_degrade_accepts_hook_timeout_kind(tmp_path):
    telemetry.record_degrade(
        kind=telemetry.KIND_HOOK_TIMEOUT, cause="took too long", engine_root=tmp_path
    )

    rows = telemetry.degrade_samples(tmp_path)
    assert rows[0]["kind"] == telemetry.KIND_HOOK_TIMEOUT


def test_record_degrade_rejects_an_unknown_kind(tmp_path):
    with pytest.raises(ValueError):
        telemetry.record_degrade(kind="something-else", cause="x", engine_root=tmp_path)

    assert telemetry.degrade_samples(tmp_path) == []


def test_absent_file_reads_as_no_degrades(tmp_path):
    assert telemetry.degrade_samples(tmp_path) == []


def test_recorder_never_raises_past_a_locking_failure(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(telemetry.locked_write, "held_lock", _boom)
    telemetry.record_degrade(
        kind=telemetry.KIND_COLD_RUN, cause="x", engine_root=tmp_path
    )


def test_multiple_rows_append_rather_than_overwrite(tmp_path):
    telemetry.record_degrade(kind=telemetry.KIND_COLD_RUN, cause="a", engine_root=tmp_path)
    telemetry.record_degrade(kind=telemetry.KIND_HOOK_TIMEOUT, cause="b", engine_root=tmp_path)

    rows = telemetry.degrade_samples(tmp_path)
    assert [r["kind"] for r in rows] == [telemetry.KIND_COLD_RUN, telemetry.KIND_HOOK_TIMEOUT]


def test_dispatch_failure_records_a_cold_run(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        telemetry, "record_degrade", lambda **kw: recorded.append(kw)
    )

    def boom(raw, *, write, **kwargs):
        raise RuntimeError("dispatch exploded")

    srv, port, _t = http_listener.start(lambda: (boom, {}))
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(port, {"method": "ping"})
        assert exc.value.code == 500
    finally:
        srv.shutdown()

    assert len(recorded) == 1
    assert recorded[0]["kind"] == telemetry.KIND_COLD_RUN
    assert "do_POST" in recorded[0]["cause"]


def test_fast_dispatch_records_nothing(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        telemetry, "record_degrade", lambda **kw: recorded.append(kw)
    )

    def fake_serve_line(raw, *, write, **kwargs):
        write(b'{"ok":true}')

    srv, port, _t = http_listener.start(lambda: (fake_serve_line, {}))
    try:
        status, _body = _post(port, {"method": "ping"})
        assert status == 200
    finally:
        srv.shutdown()

    assert recorded == []


def test_slow_dispatch_records_a_hook_timeout(monkeypatch):
    """A dispatch that clears `HOOK_BUDGET_SECS` is still answered normally
    (200), but the overrun is recorded before the caller-side harness
    timeout would ever fire."""
    recorded = []
    monkeypatch.setattr(
        telemetry, "record_degrade", lambda **kw: recorded.append(kw)
    )
    monkeypatch.setattr(http_listener, "HOOK_BUDGET_SECS", 0.0)

    def fake_serve_line(raw, *, write, **kwargs):
        write(b'{"ok":true}')

    srv, port, _t = http_listener.start(lambda: (fake_serve_line, {}))
    try:
        status, _body = _post(port, {"method": "ping"})
        assert status == 200
    finally:
        srv.shutdown()

    assert len(recorded) == 1
    assert recorded[0]["kind"] == telemetry.KIND_HOOK_TIMEOUT
    assert "do_POST" in recorded[0]["cause"]
