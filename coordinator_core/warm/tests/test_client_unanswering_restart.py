"""
coordinator_core.warm.tests.test_client_unanswering_restart -- K consecutive unanswered
calls to a live engine request a backed-off restart through `hang_restart.decide`; a
replay-safe frame then replays once, any other frame keeps the -32004 envelope.

Invoke via ``pytest coordinator_core/warm/tests/test_client_unanswering_restart.py -v``
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from coordinator_core.warm import breadcrumb, client, hang_restart
from coordinator_core.warm.engine_root import current_engine_clone

_PID = 424242
_TOKEN = "faketoken"


class _StuckPipe:
    def write(self, data: bytes) -> None:
        pass

    def flush(self) -> None:
        pass

    def readline(self):
        threading.Event().wait(30)
        return b""

    def close(self) -> None:
        pass


class _AnsweringPipe(_StuckPipe):
    def readline(self):
        return b'{"jsonrpc": "2.0", "id": 1, "result": {}}\n'


@pytest.fixture()
def engine(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path))
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: _TOKEN)
    monkeypatch.setattr(client.election, "pipe_name", lambda token: r"\\.\pipe\fake")
    monkeypatch.setattr(client, "_spawned_this_process", False)
    monkeypatch.setattr(client, "_live_tree_cold", False)
    monkeypatch.setattr(client, "_cold_reason", None)
    monkeypatch.setattr(client, "READ_DEADLINE_SECS", 0.02)
    monkeypatch.setattr(client, "_mutation_deadline_for", lambda method: 0.04)
    monkeypatch.setattr(breadcrumb, "stable_pid_alive", lambda *a, **k: True)
    svc = breadcrumb.svc_dir(current_engine_clone())
    svc.mkdir(parents=True)
    (svc / breadcrumb.BREADCRUMB_FILENAME).write_text(
        json.dumps({"pipe": "p", "pid": _PID, "stable_pid_start_epoch": 1}), encoding="utf-8"
    )
    killed: list = []
    monkeypatch.setattr(hang_restart, "exit_server", killed.append)
    decided: list = []
    real_decide = hang_restart.decide
    monkeypatch.setattr(
        hang_restart, "decide", lambda *a, **k: decided.append(k) or real_decide(*a, **k)
    )
    return svc, killed, decided


def _msg(method: str) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}


def _preseed(count: int) -> None:
    client._unanswered_path().write_text(
        json.dumps({"token": _TOKEN, "count": count, "last": time.time()}), encoding="utf-8"
    )


def _refuse_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(name):
        raise TimeoutError

    monkeypatch.setattr(client, "_open_pipe", _raise)


def test_counter_sits_beside_the_discovery_record() -> None:
    assert client._unanswered_path().parent == breadcrumb.svc_dir(current_engine_clone())


def test_k_minus_one_unanswered_connects_request_no_decision(engine, monkeypatch) -> None:
    _svc, killed, decided = engine
    _refuse_connect(monkeypatch)
    for _ in range(client.UNANSWERED_RESTART_K - 1):
        assert client.try_warm_dispatch(_msg("review.prep")) is None
    assert decided == []
    assert killed == []


def test_k_unanswered_connects_decide_once_and_end_the_engine(engine, monkeypatch) -> None:
    svc, killed, decided = engine
    _refuse_connect(monkeypatch)
    for _ in range(client.UNANSWERED_RESTART_K):
        client.try_warm_dispatch(_msg("review.prep"))
    assert len(decided) == 1
    assert killed == [_PID]
    assert "action=restart" in (svc / hang_restart.HANG_RESTART_LOG_FILENAME).read_text(encoding="utf-8")
    assert not client._unanswered_path().exists()


def test_replay_safe_frame_replays_cold_once_after_the_restart(engine, monkeypatch) -> None:
    _svc, killed, decided = engine
    _preseed(client.UNANSWERED_RESTART_K - 1)
    monkeypatch.setattr(client, "_open_pipe", lambda name: _StuckPipe())
    assert client.try_warm_dispatch(_msg("review.prep")) is None
    assert client._last_cold_bucket == client.COLD_BUCKET_MUTATION_DEADLINE_COLD_REPLAY
    assert killed == [_PID]
    assert len(decided) == 1


def test_non_replay_safe_frame_keeps_the_envelope_and_names_the_restart(engine, monkeypatch) -> None:
    _svc, killed, _decided = engine
    _preseed(client.UNANSWERED_RESTART_K - 1)
    monkeypatch.setattr(client, "_open_pipe", lambda name: _StuckPipe())
    response = client.try_warm_dispatch(_msg("ceremony.scoped_git_commit"))
    assert response["error"]["code"] == client.WARM_DISPATCH_INDETERMINATE
    assert "restart requested" in response["error"]["message"]
    assert killed == [_PID]


def test_suppressed_decision_keeps_the_envelope_and_adds_the_backoff_remaining(engine, monkeypatch) -> None:
    svc, killed, decided = engine
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (svc / hang_restart.HANG_RESTART_LOG_FILENAME).write_text(
        f"{stamp} pid=1 action=restart count=0 window_s=0 backoff_s=0 dumps=\n", encoding="utf-8"
    )
    _preseed(client.UNANSWERED_RESTART_K - 1)
    monkeypatch.setattr(client, "_open_pipe", lambda name: _StuckPipe())
    response = client.try_warm_dispatch(_msg("ceremony.scoped_git_commit"))
    assert response["error"]["code"] == client.WARM_DISPATCH_INDETERMINATE
    assert "suppressed" in response["error"]["message"]
    assert "backoff remaining" in response["error"]["message"]
    assert len(decided) == 1
    assert killed == []


def test_a_dead_pid_requests_no_decision(engine, monkeypatch) -> None:
    _svc, killed, decided = engine
    monkeypatch.setattr(breadcrumb, "stable_pid_alive", lambda *a, **k: False)
    _preseed(client.UNANSWERED_RESTART_K - 1)
    _refuse_connect(monkeypatch)
    assert client.try_warm_dispatch(_msg("review.prep")) is None
    assert decided == [] and killed == []


def test_an_answered_call_ends_the_run(engine, monkeypatch) -> None:
    _preseed(client.UNANSWERED_RESTART_K - 1)
    monkeypatch.setattr(client, "_open_pipe", lambda name: _AnsweringPipe())
    assert client.try_warm_dispatch(_msg("review.prep")) == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert not client._unanswered_path().exists()


def test_a_counter_write_failure_is_no_decision_not_an_error(engine, monkeypatch) -> None:
    _svc, killed, decided = engine

    def _boom(token, now):
        raise OSError("disk starved")

    monkeypatch.setattr(client, "_count_unanswered", _boom)
    _refuse_connect(monkeypatch)
    assert client.try_warm_dispatch(_msg("review.prep")) is None
    assert decided == [] and killed == []


def test_a_counter_write_that_overruns_its_deadline_is_no_decision(engine, monkeypatch) -> None:
    _svc, killed, decided = engine
    monkeypatch.setattr(client, "_UNANSWERED_DEADLINE_SECS", 0.05)
    monkeypatch.setattr(client, "_count_unanswered", lambda token, now: time.sleep(0.5) or 99)
    _refuse_connect(monkeypatch)
    started = time.monotonic()
    assert client.try_warm_dispatch(_msg("review.prep")) is None
    assert time.monotonic() - started < 0.4
    assert decided == [] and killed == []
