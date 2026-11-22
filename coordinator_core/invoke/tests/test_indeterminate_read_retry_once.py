"""A -32004 on a COMPUTE_ONLY op is settled by one reconcile poll, then at most one re-dispatch;
a MUTATING op is never re-dispatched by this path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.authz import classification
from coordinator_core.invoke.__main__ import _dispatch_argv

pytestmark = pytest.mark.complete

OP = "test.indeterminate_read_op"
KEY = "4242-0-63324006915000"
RESULT = {"rows": 3}


def _indeterminate(keyed: bool = True) -> dict:
    data: dict = {"retry_safe": False}
    if keyed:
        data.update({"dispatch_key": KEY, "reconcile": "warm.request_status", "engine_pid": 80932})
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32004, "message": "warm dispatch indeterminate", "data": data},
    }


def _run(monkeypatch, tmp_path: Path, op_class, replies: list, poll_reply, *, keyed: bool = True):
    """Warm leg answers `replies` in order to the op and `poll_reply` to the reconcile poll."""
    calls: list = []
    queue = list(replies)
    original = classification.classify

    def fake_classify(method):
        return op_class if method == OP else original(method)

    def warm(msg, **kw):
        calls.append(msg["method"])
        if msg["method"] == "warm.request_status":
            if isinstance(poll_reply, Exception):
                raise poll_reply
            return poll_reply
        return queue.pop(0) if queue else _indeterminate(keyed)

    monkeypatch.setattr(classification, "classify", fake_classify)
    monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", "0")
    monkeypatch.setattr(ipc, "_unstamped_dispatch_allowed", True)
    monkeypatch.delenv(ipc.PYTEST_UNSTAMPED_DISPATCH_ENV, raising=False)
    monkeypatch.setattr(ipc, "_is_dispatch_engine_stamped", lambda: True)
    monkeypatch.setattr("coordinator_core.warm.settings.is_warm_enabled", lambda: True)
    monkeypatch.setattr("coordinator_core.warm.client.try_warm_dispatch", warm)
    monkeypatch.setattr("coordinator_core.warm.client.last_cold_reason", lambda: None)
    monkeypatch.setattr("coordinator_core.warm.telemetry.record_client_boot_wait", lambda **k: None)
    return calls, _dispatch_argv([OP, "{}"], str(tmp_path), allow_warm=True)


def _ok() -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": RESULT}


def test_read_op_redispatches_once_after_an_unfinished_poll(monkeypatch, tmp_path) -> None:
    poll = {"jsonrpc": "2.0", "id": 2, "result": {"state": "executing"}}
    calls, (stdout, stderr, code) = _run(
        monkeypatch, tmp_path, classification.OpClass.COMPUTE_ONLY, [_indeterminate(), _ok()], poll
    )

    assert code == 0
    assert json.loads(stdout)["result"] == RESULT
    assert calls == [OP, "warm.request_status", OP]
    assert OP in stderr and "re-dispatching the read once" in stderr


def test_read_op_finished_poll_is_the_reply_without_redispatch(monkeypatch, tmp_path) -> None:
    poll = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"state": "finished", "outcome": "result", "result": {"rows": 9}},
    }
    calls, (stdout, _stderr, code) = _run(
        monkeypatch, tmp_path, classification.OpClass.COMPUTE_ONLY, [_indeterminate()], poll
    )

    assert code == 0
    assert json.loads(stdout)["result"] == {"rows": 9}
    assert calls == [OP, "warm.request_status"]


def test_read_op_second_indeterminate_is_returned_unchanged_and_never_looped(monkeypatch, tmp_path) -> None:
    poll = {"jsonrpc": "2.0", "id": 2, "result": {"state": "executing"}}
    calls, (_stdout, _stderr, code) = _run(
        monkeypatch,
        tmp_path,
        classification.OpClass.COMPUTE_ONLY,
        [_indeterminate(), _indeterminate()],
        poll,
    )

    assert code == 1
    assert calls == [OP, "warm.request_status", OP]


def test_read_op_failed_poll_still_redispatches_once(monkeypatch, tmp_path) -> None:
    calls, (stdout, _stderr, code) = _run(
        monkeypatch,
        tmp_path,
        classification.OpClass.COMPUTE_ONLY,
        [_indeterminate(), _ok()],
        RuntimeError("poll failed"),
    )

    assert code == 0
    assert json.loads(stdout)["result"] == RESULT
    assert calls == [OP, "warm.request_status", OP]


def test_keyless_read_op_redispatches_once_without_a_poll(monkeypatch, tmp_path) -> None:
    calls, (stdout, _stderr, code) = _run(
        monkeypatch,
        tmp_path,
        classification.OpClass.COMPUTE_ONLY,
        [_indeterminate(keyed=False), _ok()],
        None,
        keyed=False,
    )

    assert code == 0
    assert json.loads(stdout)["result"] == RESULT
    assert calls == [OP, OP]


def test_mutating_op_is_never_redispatched_by_the_read_retry(monkeypatch, tmp_path) -> None:
    poll = {"jsonrpc": "2.0", "id": 2, "result": {"state": "executing"}}
    calls, (_stdout, stderr, code) = _run(
        monkeypatch, tmp_path, classification.OpClass.MUTATING, [_indeterminate(), _ok()], poll
    )

    assert code == 1
    assert calls.count(OP) == 1
    assert "re-dispatching the read once" not in stderr
