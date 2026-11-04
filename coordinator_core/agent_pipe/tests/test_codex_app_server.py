"""Tests for agent_pipe.codex_app_server against a fake proxy replaying codex_app_server.jsonl."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from coordinator_core.agent_pipe import codex_app_server as cas
from coordinator_core.agent_pipe.tests.fixtures.fake_app_server import CAPTURE, SCRIPT

OWNER_THREAD = "01a1a3e1-30c3-71c1-9eab-aae86f8bfb81"
NON_OWNER_THREAD = "01a1a3e0-df64-7ce3-870d-b8c890ff9232"


def _argv(scenario: str) -> list[str]:
    return [sys.executable, SCRIPT, scenario]


def _capture_text(scenario: str, thread_key: str) -> str:
    """The text the captured server reported as the agent reply, read from the fixture itself."""
    for line in CAPTURE.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row["scenario"] != scenario or row["dir"] != "server":
            continue
        msg = row["msg"]
        if thread_key == "completed" and msg.get("method") == "item/completed":
            item = msg["params"]["item"]
            if item["type"] == "agentMessage":
                return item["text"]
        if thread_key == "turns" and "result" in msg and "data" in msg["result"] and msg["result"]["data"] \
                and isinstance(msg["result"]["data"][0], dict):
            for it in msg["result"]["data"][0]["items"]:
                if it["type"] == "agentMessage":
                    return it["text"]
    raise AssertionError("no agent text in capture")


def test_owner_turn_yields_agent_message_and_stops_at_turn_completed():
    got = list(cas.deliver(_argv("owner"), OWNER_THREAD, "Reply with exactly: PONG."))
    assert got == [_capture_text("owner", "completed")] == ["PONG"]


def test_non_owner_reads_reply_back_after_idle():
    got = list(cas.deliver(_argv("non_owner"), NON_OWNER_THREAD, "x"))
    assert got == [_capture_text("non_owner", "turns")] == ["CROSS"]


def test_jsonrpc_error_raises_app_server_error():
    with pytest.raises(cas.AppServerError) as ei:
        list(cas.deliver(_argv("error"), "00000000-0000-0000-0000-000000000000", "x"))
    assert ei.value.code == -32600
    assert "thread not found" in ei.value.message
    assert not isinstance(ei.value, cas.AppServerUnreachable)


def test_proxy_exit_before_initialize_is_unreachable():
    with pytest.raises(cas.AppServerUnreachable):
        list(cas.deliver(_argv("dead"), OWNER_THREAD, "x"))


def test_missing_binary_is_unreachable():
    with pytest.raises(cas.AppServerUnreachable):
        cas.loaded_threads(["/nonexistent/codex-proxy"])


def test_close_after_handshake_is_plain_error_not_unreachable():
    with pytest.raises(cas.AppServerError) as ei:
        list(cas.deliver(_argv("drop"), OWNER_THREAD, "x"))
    assert not isinstance(ei.value, cas.AppServerUnreachable)


def test_loaded_threads_returns_captured_ids():
    ids = cas.loaded_threads(_argv("owner"))
    assert "01a1a3df-65ce-7a80-a2ea-7b83a8095660" in ids
    assert len(ids) == 4


def test_handshake_and_turn_start_requests_on_the_wire(monkeypatch):
    sent: list[dict] = []
    real = cas._Conn.send

    def spy(self, obj):
        sent.append(obj)
        real(self, obj)

    monkeypatch.setattr(cas._Conn, "send", spy)
    list(cas.deliver(_argv("owner"), OWNER_THREAD, "hello"))
    assert [m.get("method") for m in sent] == ["initialize", "initialized", "thread/turns/list", "turn/start"]
    assert all(m["jsonrpc"] == "2.0" for m in sent)
    assert [m["id"] for m in sent if "id" in m] == [1, 2, 3]
    assert sent[3]["params"] == {"threadId": OWNER_THREAD, "input": [{"type": "text", "text": "hello"}]}
    assert "thread/resume" not in {m.get("method") for m in sent}


def test_child_is_killed_even_when_generator_is_abandoned(monkeypatch):
    procs = []
    real = cas.subprocess.Popen

    def spy(*a, **k):
        p = real(*a, **k)
        procs.append(p)
        return p

    monkeypatch.setattr(cas.subprocess, "Popen", spy)
    gen = cas.deliver(_argv("owner"), OWNER_THREAD, "x")
    next(gen)
    gen.close()
    assert procs and procs[0].poll() is not None


def test_frame_roundtrip_masks_client_frames():
    for size in (5, 300, 70000):
        frame = cas._frame(b"a" * size)
        assert frame[1] & 0x80
        opcode, payload = cas._read_message(io.BytesIO(frame))
        assert (opcode, payload) == (1, b"a" * size)


def _wire(monkeypatch) -> list[dict]:
    sent: list[dict] = []
    real = cas._Conn.send

    def spy(self, obj):
        sent.append(obj)
        real(self, obj)

    monkeypatch.setattr(cas._Conn, "send", spy)
    return sent


def test_busy_thread_is_steered_with_expected_turn_id(monkeypatch):
    sent = _wire(monkeypatch)
    got = list(cas.deliver(_argv("busy"), NON_OWNER_THREAD, "more"))
    methods = [m.get("method") for m in sent]
    assert "turn/steer" in methods and "turn/start" not in methods
    steer = next(m for m in sent if m.get("method") == "turn/steer")
    assert steer["params"] == {
        "threadId": NON_OWNER_THREAD,
        "input": [{"type": "text", "text": "more"}],
        "expectedTurnId": "busy-turn-0001",
    }
    assert got == ["CROSS"]


def test_idle_thread_is_started_not_steered(monkeypatch):
    sent = _wire(monkeypatch)
    list(cas.deliver(_argv("non_owner"), NON_OWNER_THREAD, "x"))
    methods = [m.get("method") for m in sent]
    assert "turn/start" in methods and "turn/steer" not in methods


def test_stale_steer_falls_back_to_turn_start(monkeypatch):
    sent = _wire(monkeypatch)
    got = list(cas.deliver(_argv("busy_stale"), NON_OWNER_THREAD, "x"))
    methods = [m.get("method") for m in sent]
    assert methods.index("turn/steer") < methods.index("turn/start")
    assert got == ["CROSS"]


def test_module_is_marked_prototype_and_import_surface_is_plain():
    assert cas.__doc__.startswith("PROTOTYPE:")
    assert callable(cas.deliver) and callable(cas.loaded_threads)
    src = Path(cas.__file__).read_text(encoding="utf-8")
    assert "coordinator_core.ops" not in src and "from coordinator_core" not in src
