"""A keyed mutating -32004 is reconciled through one `warm.request_status` poll: a terminal outcome
is the reply, anything else keeps the indeterminate envelope, and the op is never re-sent.
"""

from __future__ import annotations

import json
import threading

import pytest

from coordinator_core.warm import client

_MSG = {"jsonrpc": "2.0", "id": 7, "method": "ceremony.scoped_git_commit", "params": {}}
_KEY = "123-456"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_delivering_engine", lambda: None)
    monkeypatch.setattr(client, "_journaled_outcome", lambda key: None)
    monkeypatch.setattr(client, "_abandoned_in_import", lambda key: False)


def _poll_returns(monkeypatch: pytest.MonkeyPatch, status):
    seen: list[tuple] = []

    def _fake(pipe, token, key, engine):
        seen.append((pipe, token, key, engine))
        return status

    monkeypatch.setattr(client, "_reconcile_poll", _fake)
    return seen


def _reconcile(waited=1.0):
    return client._reconcile_or_indeterminate(_MSG, "no response within 5s", _KEY, "pipe", "tok", waited)


def test_finished_result_is_returned_as_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _poll_returns(monkeypatch, {"state": "finished", "outcome": "result", "result": {"sha": "abc"}})
    assert _reconcile() == {"jsonrpc": "2.0", "id": 7, "result": {"sha": "abc"}}
    assert seen == [("pipe", "tok", _KEY, None)]


def test_finished_error_is_returned_as_that_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _poll_returns(
        monkeypatch,
        {"state": "finished", "outcome": "error", "error_code": -32000, "failure_text": "boom"},
    )
    assert _reconcile()["error"] == {"code": -32000, "message": "boom"}


@pytest.mark.parametrize(
    "status",
    [
        {"state": "not_received", "outcome": "not-dispatched"},
        {"state": "queued"},
        {"state": "executing"},
        {"state": "executing", "overdue": True},
        {"state": "unknowable", "reason": "engine-restarted"},
        {"state": "unknowable", "reason": "killed"},
        {"state": "finished", "outcome": "result", "result_digest": {}, "result_dropped": "evicted"},
        {"state": "finished", "outcome": "error", "error_code": client.WARM_DISPATCH_INDETERMINATE},
        {"state": "finished", "outcome": "error"},
        None,
    ],
)
def test_a_non_terminal_poll_keeps_the_indeterminate_envelope(monkeypatch: pytest.MonkeyPatch, status) -> None:
    _poll_returns(monkeypatch, status)
    envelope = _reconcile()
    assert envelope["error"]["code"] == client.WARM_DISPATCH_INDETERMINATE
    assert envelope["error"]["data"]["dispatch_key"] == _KEY
    assert envelope["error"]["data"]["retry_safe"] is False


def test_an_unkeyed_envelope_is_never_polled(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _poll_returns(monkeypatch, {"state": "finished", "outcome": "result", "result": {}})
    envelope = client._reconcile_or_indeterminate(_MSG, "x", None, "pipe", "tok")
    assert envelope["error"]["code"] == client.WARM_DISPATCH_INDETERMINATE
    assert seen == []


def test_an_import_phase_refusal_is_not_polled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_abandoned_in_import", lambda key: True)
    seen = _poll_returns(monkeypatch, {"state": "finished", "outcome": "result", "result": {}})
    assert _reconcile()["error"]["data"]["import_phase"] is True
    assert seen == []


def test_poll_failure_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(name):
        raise OSError("no pipe")

    monkeypatch.setattr(client, "_open_pipe", _boom)
    assert client._reconcile_poll("pipe", "tok", _KEY, None) is None


class _Pipe:
    def __init__(self, reply: bytes | None):
        self.written: list[bytes] = []
        self._reply = reply

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass

    def readline(self) -> bytes:
        if self._reply is None:
            threading.Event().wait(30)
            return b""
        return self._reply

    def close(self) -> None:
        pass


def test_poll_is_capped_at_500ms_total_even_if_the_constant_is_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    monkeypatch.setattr(client, "RECONCILE_POLL_SECS", 30.0)
    monkeypatch.setattr(client, "_open_pipe", lambda name: _Pipe(None))
    t0 = time.monotonic()
    assert client._reconcile_poll("pipe", "tok", _KEY, None) is None
    assert time.monotonic() - t0 < 1.0


def test_a_slow_open_spends_the_poll_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    def _slow_open(name):
        time.sleep(0.55)
        return _Pipe(b'{"result": {"state": "finished", "outcome": "result", "result": {}}}\n')

    monkeypatch.setattr(client, "_open_pipe", _slow_open)
    assert client._reconcile_poll("pipe", "tok", _KEY, None) is None


@pytest.fixture()
def _warm_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: "faketoken")
    monkeypatch.setattr(client.election, "pipe_name", lambda token: r"\\.\pipe\fake")
    monkeypatch.setattr(client, "_spawned_this_process", False)
    monkeypatch.setattr(client, "_live_tree_cold", False)
    monkeypatch.setattr(client, "_cold_reason", None)
    monkeypatch.setattr(client, "READ_DEADLINE_SECS", 0.02)
    monkeypatch.setattr(client, "_mutation_deadline_for", lambda method: 0.04)


@pytest.mark.parametrize(
    ("poll_reply", "expect_result"),
    [
        (
            {"jsonrpc": "2.0", "id": "reconcile", "result": {"state": "finished", "outcome": "result", "result": {"ok": 1}}},
            True,
        ),
        ({"jsonrpc": "2.0", "id": "reconcile", "result": {"state": "executing"}}, False),
    ],
)
def test_a_delivered_mutation_that_outlives_its_deadline_polls_once_and_never_resends(
    monkeypatch: pytest.MonkeyPatch, _warm_on: None, poll_reply, expect_result: bool
) -> None:
    request_pipe = _Pipe(None)
    poll_pipe = _Pipe(json.dumps(poll_reply).encode() + b"\n")
    pipes = [request_pipe, poll_pipe]
    monkeypatch.setattr(client, "_open_pipe", lambda name: pipes.pop(0))

    response = client.try_warm_dispatch(dict(_MSG))

    assert len(request_pipe.written) == 1
    sent = json.loads(request_pipe.written[0])
    assert sent["method"] == _MSG["method"]
    assert len(poll_pipe.written) == 1
    polled = json.loads(poll_pipe.written[0])
    assert polled["method"] == "warm.request_status"
    assert polled["params"]["key"] == sent["_dispatch_key"]
    assert pipes == []
    if expect_result:
        assert response == {"jsonrpc": "2.0", "id": 7, "result": {"ok": 1}}
    else:
        assert response["error"]["code"] == client.WARM_DISPATCH_INDETERMINATE


def test_a_blocked_open_cannot_hold_the_caller_past_500ms(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    def _blocked_open(name):
        threading.Event().wait(5)
        return _Pipe(None)

    monkeypatch.setattr(client, "_open_pipe", _blocked_open)
    t0 = time.monotonic()
    assert client._reconcile_poll("pipe", "tok", _KEY, None) is None
    assert time.monotonic() - t0 < 0.8
