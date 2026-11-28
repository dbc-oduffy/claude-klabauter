"""
coordinator_core.warm.tests.test_warm_timeout_cold_replay_once -- a delivered
`review.prep` whose warm wait expires goes cold exactly once, loudly; every
other MUTATING op still returns the indeterminate envelope and never goes cold.

Invoke via ``pytest coordinator_core/warm/tests/test_warm_timeout_cold_replay_once.py -v``
"""

from __future__ import annotations

import json
import threading

import pytest

from coordinator_core.warm import client


class _StuckPipe:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass

    def readline(self):
        threading.Event().wait(30)
        return b""

    def close(self) -> None:
        pass


@pytest.fixture()
def _stuck_warm(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: "faketoken")
    monkeypatch.setattr(client.election, "pipe_name", lambda token: r"\\.\pipe\fake")
    monkeypatch.setattr(client, "_spawned_this_process", False)
    monkeypatch.setattr(client, "_live_tree_cold", False)
    monkeypatch.setattr(client, "_cold_reason", None)
    from coordinator_core.warm import breadcrumb

    monkeypatch.setattr(breadcrumb, "should_spawn", lambda engine_root=None, **kw: True)
    pipe = _StuckPipe()
    opened: list[str] = []
    monkeypatch.setattr(client, "_open_pipe", lambda name: opened.append(name) or pipe)
    monkeypatch.setattr(client, "READ_DEADLINE_SECS", 0.02)
    monkeypatch.setattr(client, "_mutation_deadline_for", lambda method: 0.04)
    return pipe, opened


def _msg(method: str) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}


def test_review_prep_deadline_expiry_goes_cold_once_and_loudly(_stuck_warm, capsys) -> None:
    pipe, opened = _stuck_warm
    response = client.try_warm_dispatch(_msg("review.prep"))
    assert response is None
    assert len(opened) == 1
    assert len(pipe.written) == 1
    assert json.loads(pipe.written[0])["method"] == "review.prep"
    assert client._last_cold_bucket == client.COLD_BUCKET_MUTATION_DEADLINE_COLD_REPLAY
    assert "review.prep" in capsys.readouterr().err


def test_other_mutating_op_still_returns_the_indeterminate_envelope(_stuck_warm) -> None:
    pipe, opened = _stuck_warm
    response = client.try_warm_dispatch(_msg("ceremony.scoped_git_commit"))
    assert response is not None
    assert response["error"]["code"] == client.WARM_DISPATCH_INDETERMINATE
    methods = [json.loads(frame)["method"] for frame in pipe.written]
    assert methods.count("ceremony.scoped_git_commit") == 1
    assert set(methods) <= {"ceremony.scoped_git_commit", client._RECONCILE_POLL_METHOD}
    assert len(opened) == len(methods)


def test_only_review_prep_is_admitted() -> None:
    assert client._COLD_REPLAY_SAFE == frozenset({"review.prep"})


def test_only_measured_rev_is_restart_resend_safe() -> None:
    assert client._RESTART_RESEND_SAFE == frozenset({"dispatch.measured_rev"})
    assert "dispatch.measured_rev" not in client._COLD_REPLAY_SAFE


class _ClosingPipe(_StuckPipe):
    def readline(self):
        threading.Event().wait(0.1)
        return b""


class _AnsweringPipe(_StuckPipe):
    def readline(self):
        return (json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"sha": "abc"}}) + "\n").encode("utf-8")


def _restarted_engine(monkeypatch, status, second_pipe):
    first = _ClosingPipe()
    pipes = [first, second_pipe]
    monkeypatch.setattr(client, "_open_pipe", lambda name: pipes.pop(0))
    monkeypatch.setattr(client, "_reconcile_poll", lambda *a, **k: status)
    monkeypatch.setattr(client, "_clear_unanswered", lambda: None)
    monkeypatch.setattr(client, "_mutation_deadline_for", lambda method: 2.0)
    monkeypatch.setattr(client, "_note_unanswered", lambda *a, **k: None)
    monkeypatch.setattr(client, "_cold_direct_while_unhealthy", lambda *a, **k: False)
    return first, second_pipe


_RESTARTED = {"state": "unknowable", "reason": "engine-restarted"}


def test_measured_rev_killed_by_a_restart_is_re_sent_once_to_the_restarted_engine(_stuck_warm, monkeypatch) -> None:
    first, second = _restarted_engine(monkeypatch, _RESTARTED, _AnsweringPipe())
    response = client.try_warm_dispatch(_msg("dispatch.measured_rev"))
    assert response["result"] == {"sha": "abc"}
    assert len(first.written) == 1 and len(second.written) == 1
    assert json.loads(second.written[0])["_dispatch_key"] != json.loads(first.written[0])["_dispatch_key"]


@pytest.mark.parametrize("reason", ["killed", "expired", "worker_dead"])
def test_measured_rev_is_not_re_sent_for_any_other_unknowable_reason(_stuck_warm, monkeypatch, reason) -> None:
    _, second = _restarted_engine(monkeypatch, {"state": "unknowable", "reason": reason}, _AnsweringPipe())
    response = client.try_warm_dispatch(_msg("dispatch.measured_rev"))
    assert response["error"]["code"] == client.WARM_DISPATCH_INDETERMINATE
    assert len(second.written) == 0


def test_another_mutating_op_is_not_re_sent_after_an_engine_restart(_stuck_warm, monkeypatch) -> None:
    _, second = _restarted_engine(monkeypatch, _RESTARTED, _AnsweringPipe())
    response = client.try_warm_dispatch(_msg("ceremony.scoped_git_commit"))
    assert response["error"]["code"] == client.WARM_DISPATCH_INDETERMINATE
    assert len(second.written) == 0


def test_a_failed_re_send_leaves_the_indeterminate_envelope(_stuck_warm, monkeypatch) -> None:
    _, second = _restarted_engine(monkeypatch, _RESTARTED, _ClosingPipe())
    response = client.try_warm_dispatch(_msg("dispatch.measured_rev"))
    assert response["error"]["code"] == client.WARM_DISPATCH_INDETERMINATE
    assert len(second.written) == 1
