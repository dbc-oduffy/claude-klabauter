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
    monkeypatch.setattr(client, "RECONCILE_SETTLE_SECS", 0.0)
    monkeypatch.setattr(client, "RECONCILE_SETTLE_INTERVAL_SECS", 0.0)


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


def _poll_sequence(monkeypatch: pytest.MonkeyPatch, statuses: list):
    seen: list = []

    def _fake(pipe, token, key, engine):
        seen.append(key)
        return statuses.pop(0) if len(statuses) > 1 else statuses[0]

    monkeypatch.setattr(client, "_reconcile_poll", _fake)
    return seen


def test_an_executing_op_is_settled_when_it_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "RECONCILE_SETTLE_SECS", 5.0)
    seen = _poll_sequence(
        monkeypatch,
        [
            {"state": "executing"},
            {"state": "queued"},
            {"state": "finished", "outcome": "result", "result": {"sha": "abc"}},
        ],
    )
    assert _reconcile() == {"jsonrpc": "2.0", "id": 7, "result": {"sha": "abc"}}
    assert seen == [_KEY, _KEY, _KEY]


def test_an_executing_op_that_finishes_in_error_returns_that_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "RECONCILE_SETTLE_SECS", 5.0)
    _poll_sequence(
        monkeypatch,
        [
            {"state": "executing"},
            {"state": "finished", "outcome": "error", "error_code": -32000, "failure_text": "boom"},
        ],
    )
    assert _reconcile()["error"] == {"code": -32000, "message": "boom"}


def test_an_executing_op_settles_from_the_journal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "RECONCILE_SETTLE_SECS", 5.0)
    monkeypatch.setattr(client, "_journaled_outcome", lambda key: {"outcome": "result", "result": {"j": 1}})
    _poll_sequence(monkeypatch, [{"state": "executing"}])
    envelope = client._reconcile_or_indeterminate(_MSG, "x", _KEY, "pipe", "tok")
    assert envelope == {"jsonrpc": "2.0", "id": 7, "result": {"j": 1}}


def test_an_op_that_never_finishes_keeps_the_envelope_after_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "RECONCILE_SETTLE_SECS", 0.05)
    monkeypatch.setattr(client, "RECONCILE_SETTLE_INTERVAL_SECS", 0.01)
    seen = _poll_sequence(monkeypatch, [{"state": "executing"}])
    envelope = _reconcile()
    assert envelope["error"]["code"] == client.WARM_DISPATCH_INDETERMINATE
    assert envelope["error"]["data"]["reconcile_command"]
    assert len(seen) > 1


@pytest.mark.parametrize(
    "status",
    [
        {"state": "unknowable", "reason": "killed"},
        {"state": "not_received", "outcome": "not-dispatched"},
        None,
    ],
)
def test_a_final_non_terminal_state_is_not_waited_on(monkeypatch: pytest.MonkeyPatch, status) -> None:
    monkeypatch.setattr(client, "RECONCILE_SETTLE_SECS", 5.0)
    seen = _poll_sequence(monkeypatch, [status])
    assert _reconcile()["error"]["code"] == client.WARM_DISPATCH_INDETERMINATE
    assert len(seen) == 1


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


def test_a_dropped_reply_for_an_executing_op_is_settled_through_the_door(
    monkeypatch: pytest.MonkeyPatch, _warm_on: None
) -> None:
    monkeypatch.setattr(client, "RECONCILE_SETTLE_SECS", 5.0)
    monkeypatch.setattr(client, "RECONCILE_SETTLE_INTERVAL_SECS", 0.01)
    request_pipe = _Pipe(None)
    executing = _Pipe(b'{"jsonrpc": "2.0", "id": "reconcile", "result": {"state": "executing"}}\n')
    finished = _Pipe(
        b'{"jsonrpc": "2.0", "id": "reconcile", "result": {"state": "finished", "outcome": "result", "result": {"ok": 2}}}\n'
    )
    pipes = [request_pipe, executing, finished]
    monkeypatch.setattr(client, "_open_pipe", lambda name: pipes.pop(0))

    response = client.try_warm_dispatch(dict(_MSG))

    assert response == {"jsonrpc": "2.0", "id": 7, "result": {"ok": 2}}
    assert len(request_pipe.written) == 1
    assert pipes == []


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
