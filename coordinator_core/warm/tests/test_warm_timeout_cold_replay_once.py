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
    assert len(opened) == 1
    assert len(pipe.written) == 1


def test_only_review_prep_is_admitted() -> None:
    assert client._COLD_REPLAY_SAFE == frozenset({"review.prep"})
