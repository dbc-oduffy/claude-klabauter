"""Workflow notices queue into Codex without starting another thread writer."""
from contextlib import contextmanager

import pytest

from coordinator_core.agent_pipe import bridge, codex_app_server, roster
from coordinator_core.ops.workflow_fire.notify import notify_terminal


def test_enqueue_uses_experimental_persistent_queue(monkeypatch):
    calls = []

    class Conn:
        def request(self, method, params, deadline):
            calls.append((method, params))
            return {"queuedSubmission": {"id": "q1"}}

    @contextmanager
    def connect(argv, *, experimental=False, timeout=None):
        assert argv == ["proxy"] and experimental is True and timeout == 0.2
        yield Conn()

    monkeypatch.setattr(codex_app_server, "_connect", connect)
    assert codex_app_server.enqueue(["proxy"], "t1", "done", "m1") == "q1"
    assert calls == [("thread/queue/add", {
        "threadId": "t1", "clientUserMessageId": "m1",
        "input": [{"type": "text", "text": "done"}],
    })]


def test_workflow_completion_waits_for_queue_acceptance(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    roster.bind("codex-peer", "codex", "t1", argv=["proxy"])
    calls = []
    monkeypatch.setattr(codex_app_server, "enqueue", lambda *a: calls.append(a) or "q1")
    monkeypatch.setattr(codex_app_server, "deliver", lambda *a: pytest.fail("started a competing turn"))
    rec = {"fire_id": "f1", "state": "exited", "notify_to": "codex-peer"}
    done = notify_terminal(rec)
    assert done["notify_status"] == "queued" and done["notified_at"]
    assert calls[0][:2] == (["proxy"], "t1")
    assert "driver-done" in calls[0][2]
    notify_terminal(done)
    assert len(calls) == 1


def test_queue_failure_remains_retryable(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    roster.bind("codex-peer", "codex", "t1", argv=["proxy"])

    def fail(*args):
        raise codex_app_server.AppServerError(-1, "offline")

    monkeypatch.setattr(codex_app_server, "enqueue", fail)
    done = notify_terminal({"fire_id": "f1", "state": "exited", "notify_to": "codex-peer"})
    assert "notified_at" not in done and "offline" in done["notify_error"]


def test_muted_peer_is_not_queued(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    roster.bind("codex-peer", "codex", "t1", argv=["proxy"])
    bridge.set_mute("codex-peer")
    monkeypatch.setattr(codex_app_server, "enqueue", lambda *a: pytest.fail("ignored mute"))
    done = notify_terminal({"fire_id": "f1", "state": "exited", "notify_to": "codex-peer"})
    assert done["notify_status"] == "refused" and "notified_at" not in done
