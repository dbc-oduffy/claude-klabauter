"""Tests for agent_pipe.codex_app_server against a fake proxy replaying codex_app_server.jsonl."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

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


def test_proxy_child_env_carries_codex_vendor_and_no_inherited_model_or_session(monkeypatch):
    seen = []
    real = cas.subprocess.Popen

    def spy(*a, **k):
        seen.append(k["env"])
        return real(*a, **k)

    monkeypatch.setattr(cas.subprocess, "Popen", spy)
    monkeypatch.setenv("COORDINATOR_AGENT_VENDOR", "grok")
    monkeypatch.setenv("COORDINATOR_AGENT_MODEL", "stale")
    monkeypatch.setenv("COORDINATOR_AGENT_SESSION", "stale")
    gen = cas.deliver(_argv("owner"), OWNER_THREAD, "x")
    next(gen)
    gen.close()
    assert seen[0]["COORDINATOR_AGENT_VENDOR"] == "codex"
    assert "COORDINATOR_AGENT_MODEL" not in seen[0] and "COORDINATOR_AGENT_SESSION" not in seen[0]


def test_module_is_marked_prototype_and_import_surface_is_plain():
    assert cas.__doc__.startswith("PROTOTYPE:")
    assert callable(cas.deliver) and callable(cas.loaded_threads)
    src = Path(cas.__file__).read_text(encoding="utf-8")
    assert "coordinator_core.ops" not in src
    assert {ln.split()[1] for ln in src.splitlines() if ln.startswith("from coordinator_core")} == {
        "coordinator_core.agent_pipe.acp",
        "coordinator_core.agent_pipe.contract",
        "coordinator_core.agent_pipe.session_driver",
    }


class _ScriptConn:
    """Stand-in for _Conn: `results` maps method -> result, `inbox` feeds next_message; records sends/requests."""

    def __init__(self, results, inbox=()):
        self.results = results
        self.inbox = list(inbox)
        self.requests: list[tuple[str, dict]] = []
        self.sent: list[dict] = []

    def request(self, method, params, deadline, unreachable=False):
        self.requests.append((method, params))
        return self.results[method]

    def next_message(self, deadline, unreachable=False):
        return self.inbox.pop(0)

    def send(self, obj):
        self.sent.append(obj)


def _patch_conn(monkeypatch, conn):
    from contextlib import contextmanager

    @contextmanager
    def fake(argv):
        yield conn

    monkeypatch.setattr(cas, "_connect", fake)


def test_driver_start_sends_thread_start_on_request_policy_and_returns_handle(monkeypatch):
    sent = _wire(monkeypatch)
    h = cas.CodexAppServerDriver(_argv("owner")).start("/work")
    assert h.session_id == OWNER_THREAD and h.cwd == "/work"
    start = next(m for m in sent if m.get("method") == "thread/start")
    assert start["params"] == {"cwd": "/work", "approvalPolicy": "on-request"}


def test_driver_prompt_streams_messages_then_done():
    d = cas.CodexAppServerDriver(_argv("owner"))
    ups = list(d.prompt(cas.SessionHandle(OWNER_THREAD, "/w"), "Reply with exactly: PONG."))
    assert [(u.kind, u.text) for u in ups] == [("message", "PONG"), ("done", "")]
    assert ups[-1].raw == {"stopReason": "end_turn"}


def test_driver_resume_calls_thread_resume_and_takes_cwd_from_thread(monkeypatch):
    conn = _ScriptConn({"thread/resume": {"thread": {"id": "T", "cwd": "/there"}}})
    _patch_conn(monkeypatch, conn)
    h = cas.CodexAppServerDriver(["x"]).resume("T")
    assert (h.session_id, h.cwd) == ("T", "/there")
    assert conn.requests[0][0] == "thread/resume" and conn.requests[0][1]["threadId"] == "T"


def test_driver_cancel_interrupts_the_running_turn(monkeypatch):
    conn = _ScriptConn({
        "thread/turns/list": {"data": [{"id": "turn-9", "status": "inProgress"}]},
        "turn/interrupt": {},
    })
    _patch_conn(monkeypatch, conn)
    cas.CodexAppServerDriver(["x"]).cancel(cas.SessionHandle("T", "/w"))
    assert conn.requests[-1] == ("turn/interrupt", {"threadId": "T", "turnId": "turn-9"})


def test_driver_cancel_with_no_running_turn_sends_no_interrupt(monkeypatch):
    conn = _ScriptConn({"thread/turns/list": {"data": [{"id": "t", "status": "completed"}]}})
    _patch_conn(monkeypatch, conn)
    cas.CodexAppServerDriver(["x"]).cancel(cas.SessionHandle("T", "/w"))
    assert [m for m, _ in conn.requests] == ["thread/turns/list"]


def _approval_run(monkeypatch, decide, drive=True):
    approval = {"id": 77, "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "T", "turnId": "t1", "command": "rm -rf x"}}
    done = {"method": "turn/completed", "params": {"threadId": "T", "turn": {
        "id": "t1", "status": "completed", "items": [{"type": "agentMessage", "text": "ok"}]}}}
    conn = _ScriptConn({"thread/turns/list": {"data": []}, "turn/start": {"turn": {"id": "t1"}}}, [approval, done])
    _patch_conn(monkeypatch, conn)
    if drive:
        d = cas.CodexAppServerDriver(["x"], approve=decide)
        assert [u.kind for u in d.prompt(cas.SessionHandle("T", "/w"), "go")] == ["message", "done"]
    else:
        assert list(cas.deliver(["x"], "T", "go")) == ["ok"]
    return conn.sent


def test_approval_defaults_to_decline(monkeypatch):
    sent = _approval_run(monkeypatch, None)
    assert sent == [{"jsonrpc": "2.0", "id": 77, "result": {"decision": "decline"}}]


def test_approval_allow_once_accepts_and_allow_always_accepts_for_session(monkeypatch):
    from coordinator_core.agent_pipe.session_driver import ALLOW_ALWAYS, ALLOW_ONCE, Decision

    assert _approval_run(monkeypatch, lambda r: Decision(ALLOW_ONCE))[0]["result"] == {"decision": "accept"}
    assert _approval_run(monkeypatch, lambda r: Decision(ALLOW_ALWAYS))[0]["result"] == {"decision": "acceptForSession"}


def test_approval_callback_that_raises_declines(monkeypatch):
    def boom(request):
        raise RuntimeError("x")

    assert _approval_run(monkeypatch, boom)[0]["result"] == {"decision": "decline"}


def test_plain_deliver_without_on_approval_answers_nothing(monkeypatch):
    assert _approval_run(monkeypatch, None, drive=False) == []
