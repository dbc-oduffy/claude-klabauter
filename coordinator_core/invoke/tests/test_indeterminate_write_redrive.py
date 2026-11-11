"""An indeterminate (-32004) warm dispatch of a write op is settled by one reconcile poll:
completed -> that result is the reply; never-ran -> one cold re-drive; unknown -> the hint."""

from __future__ import annotations

import json
import os

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.invoke.__main__ import _dispatch_argv

pytestmark = pytest.mark.complete

OP = "test.indeterminate_write_op"
KEY = "4242-0-63324006915000"


@pytest.fixture()
def draft_path(tmp_path):
    path = tmp_path / "draft.md"

    def _write_op(params, ctx=None, repo_root=None):
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "w") as fh:
            fh.write("draft")
        return {"written": True}

    saved = ipc._REGISTRY.get(OP)
    ipc._REGISTRY[OP] = _write_op
    yield path
    if saved is None:
        ipc._REGISTRY.pop(OP, None)
    else:
        ipc._REGISTRY[OP] = saved


def _indeterminate() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {
            "code": -32004,
            "message": "warm dispatch indeterminate",
            "data": {
                "retry_safe": False,
                "dispatch_key": KEY,
                "reconcile": "warm.request_status",
                "engine_pid": 80932,
            },
        },
    }


def _run(monkeypatch, tmp_path, poll_reply):
    """Warm leg answers -32004 for the op and `poll_reply` for the reconcile poll."""
    calls: list = []

    def warm(msg, **kw):
        calls.append(msg["method"])
        if msg["method"] == "warm.request_status":
            assert msg["params"]["key"] == KEY
            assert msg["params"]["expect_engine"] == {"pid": 80932}
            if isinstance(poll_reply, Exception):
                raise poll_reply
            return poll_reply
        return _indeterminate()

    monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", "0")
    monkeypatch.setattr(ipc, "_unstamped_dispatch_allowed", True)
    monkeypatch.delenv(ipc.PYTEST_UNSTAMPED_DISPATCH_ENV, raising=False)
    monkeypatch.setattr(ipc, "_is_dispatch_engine_stamped", lambda: True)
    monkeypatch.setattr("coordinator_core.warm.settings.is_warm_enabled", lambda: True)
    monkeypatch.setattr("coordinator_core.warm.client.try_warm_dispatch", warm)
    monkeypatch.setattr("coordinator_core.warm.client.last_cold_reason", lambda: None)
    monkeypatch.setattr("coordinator_core.warm.telemetry.record_client_boot_wait", lambda **k: None)
    return calls, _dispatch_argv([OP, "{}"], str(tmp_path), allow_warm=True)


def test_memo_draft_is_not_classified_compute_only():
    from coordinator_core.authz.classification import OpClass, classify

    assert classify("memo.draft") is not OpClass.COMPUTE_ONLY


def test_never_ran_is_redriven_cold_once_and_the_draft_is_written(monkeypatch, tmp_path, draft_path):
    poll = {"jsonrpc": "2.0", "id": 2, "result": {"state": "not_received", "outcome": "not-dispatched"}}
    calls, (stdout, stderr, code) = _run(monkeypatch, tmp_path, poll)

    assert code == 0
    assert draft_path.read_text() == "draft"
    assert calls == [OP, "warm.request_status"]
    assert "re-driving it cold once" in stderr
    assert "Reconcile via" not in stderr


def test_completed_is_the_reply_and_nothing_is_redriven(monkeypatch, tmp_path, draft_path):
    result = {"written": True, "path": "elsewhere"}
    poll = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"state": "finished", "outcome": "result", "result": result},
    }
    calls, (stdout, stderr, code) = _run(monkeypatch, tmp_path, poll)

    assert code == 0
    assert json.loads(stdout)["result"] == result
    assert not draft_path.exists(), "a completed op must not be re-run"


@pytest.mark.parametrize(
    "poll",
    [
        {"jsonrpc": "2.0", "id": 2, "result": {"state": "unknowable", "reason": "abandoned"}},
        {"jsonrpc": "2.0", "id": 2, "result": {"state": "executing"}},
        {"jsonrpc": "2.0", "id": 2, "result": {"state": "finished", "outcome": "result", "result_dropped": "over-cap"}},
        {"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "boom"}},
        None,
        RuntimeError("poll failed"),
    ],
)
def test_unknown_or_failed_reconcile_prints_the_hint_and_never_redrives(
    monkeypatch, tmp_path, draft_path, poll
):
    calls, (stdout, stderr, code) = _run(monkeypatch, tmp_path, poll)

    assert code == 1
    assert not draft_path.exists()
    assert "Reconcile via" in stderr
    assert calls.count(OP) == 1
