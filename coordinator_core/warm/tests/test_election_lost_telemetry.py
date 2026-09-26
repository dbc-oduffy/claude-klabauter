
from __future__ import annotations

import pytest

from coordinator_core.warm import election, server, telemetry


def test_row_carries_endpoint_token_pid_and_interval(tmp_path):
    telemetry.record_election_lost(
        endpoint=r"\\.\pipe\coordinator-warm-abc",
        token="abc",
        pid=4321,
        lost_secs=0.1234,
        engine_root=tmp_path,
    )

    rows = telemetry.election_lost_samples(tmp_path)
    assert len(rows) == 1
    assert rows[0]["endpoint"] == r"\\.\pipe\coordinator-warm-abc"
    assert rows[0]["token"] == "abc"
    assert rows[0]["pid"] == 4321
    assert rows[0]["lost_secs"] == 0.123


def test_unmeasurable_interval_is_omitted_not_zeroed(tmp_path):
    telemetry.record_election_lost(endpoint="sock", engine_root=tmp_path)

    row = telemetry.election_lost_samples(tmp_path)[0]
    assert "lost_secs" not in row
    assert "token" not in row
    assert "pid" not in row


def test_absent_file_reads_as_no_losses(tmp_path):
    assert telemetry.election_lost_samples(tmp_path) == []


def test_recorder_never_raises(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(telemetry.locked_write, "held_lock", _boom)
    telemetry.record_election_lost(endpoint="sock", engine_root=tmp_path)


def test_run_guarded_records_the_loss_and_still_exits_zero(tmp_path, monkeypatch):
    recorded: list = []

    monkeypatch.setattr(server, "_engine_clone_root", lambda: tmp_path)
    monkeypatch.setattr(server.skew, "compute_client_token", lambda root: "tok-1")
    monkeypatch.setattr(server, "_spawn_epoch_from_env", lambda: 1000.0)
    monkeypatch.setattr(server.time, "time", lambda: 1002.5)

    def _lose(*args, **kwargs):
        raise election.ElectionLost("the-endpoint")

    monkeypatch.setattr(server, "_elect_windows_pipe", _lose)
    monkeypatch.setattr(server, "_elect_unix_socket_endpoint", _lose)
    monkeypatch.setattr(telemetry, "record_election_lost", lambda **kw: recorded.append(kw))

    assert server._run_guarded() == 0
    assert len(recorded) == 1
    assert recorded[0]["endpoint"] == "the-endpoint"
    assert recorded[0]["token"] == "tok-1"
    assert recorded[0]["lost_secs"] == pytest.approx(2.5)
    assert recorded[0]["engine_root"] == tmp_path


def test_a_failing_instrument_never_stops_a_clean_loss(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_engine_clone_root", lambda: tmp_path)
    monkeypatch.setattr(server.skew, "compute_client_token", lambda root: "tok-1")
    monkeypatch.setattr(server, "_spawn_epoch_from_env", lambda: None)

    def _lose(*args, **kwargs):
        raise election.ElectionLost("the-endpoint")

    monkeypatch.setattr(server, "_elect_windows_pipe", _lose)
    monkeypatch.setattr(server, "_elect_unix_socket_endpoint", _lose)
    monkeypatch.setattr(telemetry.locked_write, "held_lock", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))

    assert server._run_guarded() == 0
    assert telemetry.election_lost_samples(tmp_path) == []
