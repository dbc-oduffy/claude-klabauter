"""
coordinator_core.warm.tests.test_indeterminate_envelope_names_the_poll --
pins C5 of docs/plans/2026-09-23-warm-dispatch-reconcile.md: the client
mints and sends a dispatch key on every request, every client-produced
-32004 (`_indeterminate_envelope`) carries `error.data.dispatch_key` and
names the poll op (`error.data.reconcile`), and its message text names the
exact poll invocation. Also pins that `try_warm_dispatch` puts the same
key on the wire frame it writes.

Negative spec pinned here too: the delivery claim stays withheld -- adding
the poll route does not resurrect "may have COMPLETED" into a claim the
op DID run; it only tells the caller how to ask.

Spec backlink: docs/plans/2026-09-23-warm-dispatch-reconcile.md § C5
Contract: coordinator_core/contract/dispatch-ack-reconcile-contract.md § 1, § 9

Test convention: pytest. Invoke via
``pytest coordinator_core/warm/tests/test_indeterminate_envelope_names_the_poll.py -v``
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.warm import client

_MSG = {"jsonrpc": "2.0", "id": 1, "method": "some.mutating.op", "params": {}}


def test_envelope_without_a_key_omits_data() -> None:
    """Back-compat / unkeyed-caller shape: no `dispatch_key` argument means
    no `error.data` at all -- an old call site that has not been threaded
    through yet must not fabricate a null key."""
    envelope = client._indeterminate_envelope(_MSG, "no response within 5s")
    assert "data" not in envelope["error"]


def test_envelope_with_a_key_carries_dispatch_key_and_reconcile() -> None:
    envelope = client._indeterminate_envelope(_MSG, "no response within 5s", "123-456")
    assert envelope["error"]["data"]["dispatch_key"] == "123-456"
    assert envelope["error"]["data"]["reconcile"] == "warm.request_status"


def test_envelope_message_names_the_exact_poll_invocation() -> None:
    envelope = client._indeterminate_envelope(_MSG, "no response within 5s", "123-456")
    message = envelope["error"]["message"]
    assert "python3 -m coordinator_core.invoke warm.request_status" in message
    assert json.dumps({"key": "123-456"}) in message


def test_delivery_claim_stays_withheld_even_with_a_key() -> None:
    """Negative spec (C5 body): the poll, not this message, is what now
    knows -- adding the key must not restore "the op ran" as a claim."""
    envelope = client._indeterminate_envelope(_MSG, "no response within 5s", "123-456")
    message = envelope["error"]["message"]
    assert "finding no trace means it is safe to re-run" not in message
    assert "may have COMPLETED" in message  # unchanged uncertainty, not a claim


def test_poll_command_is_exactly_reproducible_for_a_given_key() -> None:
    assert client._poll_command_for("123-456") == (
        "python3 -m coordinator_core.invoke warm.request_status "
        "'{\"key\": \"123-456\"}' --bare"
    )


class _FakePipe:
    def __init__(self, read_result=b'{"jsonrpc":"2.0","id":1,"result":{}}\n'):
        self.written = []
        self._read_result = read_result

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass

    def readline(self):
        return self._read_result

    def close(self) -> None:
        pass


@pytest.fixture()
def _warm_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: "faketoken")
    monkeypatch.setattr(client.election, "pipe_name", lambda token: r"\\.\pipe\fake")
    monkeypatch.setattr(client, "_spawned_this_process", False)
    monkeypatch.setattr(client, "_live_tree_cold", False)
    monkeypatch.setattr(client, "_cold_reason", None)
    from coordinator_core.warm import breadcrumb

    monkeypatch.setattr(breadcrumb, "should_spawn", lambda engine_root=None, **kw: True)


def test_try_warm_dispatch_mints_and_sends_one_key_per_request(
    monkeypatch: pytest.MonkeyPatch, _warm_on: None
) -> None:
    """D1/contract § 1: one key minted per request, sent as `_dispatch_key`,
    never the JSON-RPC `id` (every caller sends `id: 1`)."""
    pipe = _FakePipe()
    monkeypatch.setattr(client, "_open_pipe", lambda name: pipe)
    client.try_warm_dispatch({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
    assert len(pipe.written) == 1
    sent = json.loads(pipe.written[0])
    assert isinstance(sent.get("_dispatch_key"), str) and sent["_dispatch_key"]
    assert sent["_dispatch_key"] != "1"


def test_try_warm_dispatch_mints_a_fresh_key_every_call(
    monkeypatch: pytest.MonkeyPatch, _warm_on: None
) -> None:
    pipe_a, pipe_b = _FakePipe(), _FakePipe()
    pipes = [pipe_a, pipe_b]
    monkeypatch.setattr(client, "_open_pipe", lambda name: pipes.pop(0))
    msg = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}
    client.try_warm_dispatch(msg)
    client.try_warm_dispatch(msg)
    key_a = json.loads(pipe_a.written[0])["_dispatch_key"]
    key_b = json.loads(pipe_b.written[0])["_dispatch_key"]
    assert key_a != key_b


def test_delivered_mutation_indeterminate_carries_the_sent_dispatch_key(
    monkeypatch: pytest.MonkeyPatch, _warm_on: None
) -> None:
    """The end-to-end shape AC-4 checks: a mutation delivered-then-unanswered
    returns a -32004 whose `error.data.dispatch_key` is the SAME key that
    was sent on the wire frame, not a fresh one."""
    import threading

    class _StuckPipe(_FakePipe):
        def readline(self):
            threading.Event().wait(30)
            return b""

    pipe = _StuckPipe()
    monkeypatch.setattr(client, "_open_pipe", lambda name: pipe)
    monkeypatch.setattr(client, "READ_DEADLINE_SECS", 0.02)
    monkeypatch.setattr(client, "_mutation_deadline_for", lambda method: 0.04)

    msg = {"jsonrpc": "2.0", "id": 1, "method": "ceremony.scoped_git_commit", "params": {}}
    response = client.try_warm_dispatch(msg)

    assert response is not None
    assert response["error"]["code"] == client.WARM_DISPATCH_INDETERMINATE
    sent = json.loads(pipe.written[0])
    assert response["error"]["data"]["dispatch_key"] == sent["_dispatch_key"]
    assert response["error"]["data"]["reconcile"] == "warm.request_status"
