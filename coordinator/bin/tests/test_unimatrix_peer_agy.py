"""Behavior pins for the agy peer adapter and the agy-run leg."""
import asyncio
import importlib.util
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if sys.platform == "win32":
    pytest.skip("fake leg uses an AF_UNIX socket", allow_module_level=True)

agy = load("unimatrix-peer-agy")
leg = load("unimatrix-agy-leg")


def row(content, source="MODEL", type_="PLANNER_RESPONSE", tool_calls=None):
    r = {"source": source, "type": type_, "content": content}
    if tool_calls:
        r["tool_calls"] = tool_calls
    return json.dumps(r) + "\n"


class FakeLeg:
    """Listens on a relative socket in cwd (short path), records requests, appends transcript rows."""

    def __init__(self, name, ack, on_text=None):
        self.name, self.ack, self.on_text, self.seen = name, ack, on_text, []
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(name)
        self.srv.listen(2)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            with c:
                buf = b""
                while not buf.endswith(b"\n"):
                    buf += c.recv(4096)
                text = json.loads(buf)["text"]
                self.seen.append(text)
                if self.on_text:
                    self.on_text(text)
                c.sendall((json.dumps(self.ack) + "\n").encode())

    def close(self):
        self.srv.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(row("old reply") + row("user echo", source="USER", type_="USER_INPUT"))
    made = []

    def make(ack, on_text=None):
        f = FakeLeg("leg.sock", ack, on_text)
        made.append(f)
        return f

    yield transcript, make
    for f in made:
        f.close()


def append(path, *lines):
    with open(path, "a", encoding="utf-8") as f:
        f.write("".join(lines))


def test_turn_returns_first_final_reply_after_baseline_skipping_tool_calls(env):
    transcript, make = env
    fake = make({"ok": True}, lambda t: append(
        transcript, row("thinking", tool_calls=[{"n": 1}]), row("final answer"), row("later")))
    out = asyncio.run(agy.agy_turn("leg.sock", transcript, "hello", timeout=5, interval=0.02))
    assert out == "final answer"
    assert fake.seen == ["hello"]


def test_turn_ignores_replies_older_than_the_baseline(env):
    transcript, make = env
    make({"ok": True})
    out = asyncio.run(agy.agy_turn("leg.sock", transcript, "hi", timeout=0.2, interval=0.02))
    assert out == agy.NO_REPLY


def test_turn_reports_failed_delivery_without_polling(env):
    transcript, make = env
    make({"ok": False, "status": 403, "body": "csrf"})
    out = asyncio.run(agy.agy_turn("leg.sock", transcript, "hi", timeout=30, interval=30))
    assert out.startswith("[unimatrix-peer-agy] delivery failed") and "403" in out


def test_turn_waits_for_a_reply_that_lands_late(env):
    transcript, make = env
    make({"ok": True})

    async def run():
        task = asyncio.create_task(agy.agy_turn("leg.sock", transcript, "hi", timeout=5, interval=0.02))
        await asyncio.sleep(0.15)
        append(transcript, row("late final"))
        return await task

    assert asyncio.run(run()) == "late final"


def test_rows_tolerates_a_missing_transcript(tmp_path):
    assert agy.rows(tmp_path / "absent.jsonl") == []


def test_transcript_root_honours_env_override_else_home(monkeypatch, tmp_path):
    monkeypatch.setenv("AGY_BRAIN_DIR", str(tmp_path))
    assert agy.transcript_path("c1") == tmp_path / "c1" / ".system_generated" / "logs" / "transcript_full.jsonl"
    monkeypatch.delenv("AGY_BRAIN_DIR")
    assert agy.brain_dir() == Path.home() / ".gemini" / "antigravity-cli" / "brain"


def test_leg_endpoint_is_roster_shaped_per_platform():
    assert agy.leg_endpoint("c1", "win32").startswith("\\\\.\\pipe\\LOCAL\\cc-msg-")
    assert agy.leg_endpoint("c1", "linux").endswith(".sock")
    assert agy.leg_endpoint("c1", "linux") != agy.leg_endpoint("c2", "linux")


def test_leg_send_uses_token_as_header_and_never_logs_it(monkeypatch, tmp_path):
    token = "SECRET-CSRF-TOKEN"
    monkeypatch.setattr(leg, "LOG", str(tmp_path / "leg.log"))
    monkeypatch.setenv("ANTIGRAVITY_CSRF_TOKEN", token)
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", "conv-1")
    monkeypatch.setenv("ANTIGRAVITY_LS_ADDRESS", "127.0.0.1:1")
    monkeypatch.delenv("AGY_LS_HTTP", raising=False)
    captured = {}

    class Resp:
        status = 200

        def read(self, n):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data)
        return Resp()

    monkeypatch.setattr(leg.urllib.request, "urlopen", fake_urlopen)
    resp = leg.send("hi agy")
    assert resp["ok"] and captured["body"] == {"cascadeId": "conv-1", "items": [{"text": "hi agy"}]}
    assert captured["headers"]["X-codeium-csrf-token"] == token
    assert token not in json.dumps(resp)
    assert token not in (tmp_path / "leg.log").read_text()


def test_leg_send_without_ls_address_fails_closed(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_CSRF_TOKEN", "t")
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", "c")
    monkeypatch.delenv("ANTIGRAVITY_LS_ADDRESS", raising=False)
    monkeypatch.delenv("AGY_LS_HTTP", raising=False)
    assert leg.send("x")["ok"] is False


def test_leg_main_exits_without_a_conversation_id(monkeypatch, tmp_path):
    monkeypatch.setattr(leg, "LOG", str(tmp_path / "leg.log"))
    monkeypatch.delenv("ANTIGRAVITY_CONVERSATION_ID", raising=False)
    monkeypatch.setenv("ANTIGRAVITY_CSRF_TOKEN", "t")
    with pytest.raises(SystemExit):
        leg.main(str(tmp_path / "s.sock"))


def start_leg(monkeypatch, tmp_path, token):
    """Run leg.main in a daemon thread on a relative socket; returns the spool path."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(leg, "LOG", str(tmp_path / "conv-1.log"))
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", "conv-1")
    monkeypatch.delenv("AGY_LEG_SPOOL", raising=False)
    monkeypatch.delenv("AGY_LS_HTTP", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_LS_ADDRESS", raising=False)
    if token:
        monkeypatch.setenv("ANTIGRAVITY_CSRF_TOKEN", token)
    else:
        monkeypatch.delenv("ANTIGRAVITY_CSRF_TOKEN", raising=False)
    start_leg.thread = threading.Thread(target=leg.main, args=("pend.sock",), daemon=True)
    start_leg.thread.start()
    for _ in range(200):
        if (tmp_path / "pend.sock").exists():
            break
        time.sleep(0.01)
    return tmp_path / "conv-1.queue.jsonl"


def test_pending_leg_without_token_queues_inbound_messages_in_a_0600_spool(monkeypatch, tmp_path):
    spool = start_leg(monkeypatch, tmp_path, None)
    assert agy.to_leg("pend.sock", "one") == {"ok": True, "queued": True}
    assert agy.to_leg("pend.sock", "two") == {"ok": True, "queued": True}
    assert [json.loads(line)["text"] for line in spool.read_text().splitlines()] == ["one", "two"]
    if sys.platform != "win32":
        assert (spool.stat().st_mode & 0o777) == 0o600


def test_pending_spool_refuses_messages_past_its_byte_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(leg, "SPOOL_MAX_BYTES", 64)
    spool = start_leg(monkeypatch, tmp_path, None)
    assert agy.to_leg("pend.sock", "a" * 10)["queued"] is True
    refused = agy.to_leg("pend.sock", "b" * 100)
    assert refused["ok"] is False and "spool" in refused["body"]
    assert [json.loads(line)["text"] for line in spool.read_text().splitlines()] == ["a" * 10]


def test_a_message_sent_before_the_leg_is_live_is_delivered_when_it_comes_up(monkeypatch, tmp_path):
    spool = start_leg(monkeypatch, tmp_path, None)
    agy.to_leg("pend.sock", "early")
    assert spool.exists()
    monkeypatch.setenv("ANTIGRAVITY_CSRF_TOKEN", "t")
    monkeypatch.setenv("ANTIGRAVITY_LS_ADDRESS", "127.0.0.1:1")
    sent = []
    monkeypatch.setattr(leg, "send", lambda text: sent.append(text) or {"ok": True})
    leg.drain()
    assert sent == ["early"] and spool.read_text() == ""


def test_drain_keeps_undelivered_and_unparseable_lines_and_preserves_order(monkeypatch, tmp_path):
    monkeypatch.setattr(leg, "LOG", str(tmp_path / "conv-1.log"))
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", "conv-1")
    monkeypatch.delenv("AGY_LEG_SPOOL", raising=False)
    spool = tmp_path / "conv-1.queue.jsonl"
    spool.write_text('{"text": "a"}\nnot json\n{"text": "b"}\n{"text": "c"}\n')
    sent = []

    def flaky(text):
        sent.append(text)
        return {"ok": text == "a"}

    monkeypatch.setattr(leg, "send", flaky)
    leg.drain()
    assert sent == ["a", "b"]
    assert spool.read_text() == 'not json\n{"text": "b"}\n{"text": "c"}\n'


def test_live_leg_delivers_a_line_a_pending_leg_appended_after_the_first_drain(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(leg, "LOG", str(tmp_path / "conv-1.log"))
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", "conv-1")
    monkeypatch.setenv("ANTIGRAVITY_CSRF_TOKEN", "t")
    monkeypatch.setenv("ANTIGRAVITY_LS_ADDRESS", "127.0.0.1:1")
    monkeypatch.delenv("AGY_LEG_SPOOL", raising=False)
    spool = tmp_path / "conv-1.queue.jsonl"
    sent = []
    monkeypatch.setattr(leg, "send", lambda text: sent.append(text) or {"ok": True})
    real_drain, calls = leg.drain, []

    def racing_drain():
        real_drain()
        calls.append(1)
        if len(calls) == 1:  # the SIGTERMed pending leg lands its append after the pre-bind drain
            spool.write_text('{"text": "late"}\n')

    monkeypatch.setattr(leg, "drain", racing_drain)
    threading.Thread(target=leg.main, args=("live.sock",), daemon=True).start()
    for _ in range(200):
        if sent:
            break
        time.sleep(0.01)
    assert sent == ["late"] and len(calls) == 2


def test_drain_with_no_spool_is_a_no_op(monkeypatch, tmp_path):
    monkeypatch.setattr(leg, "LOG", str(tmp_path / "conv-1.log"))
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", "conv-1")
    leg.drain()
    assert not (tmp_path / "conv-1.queue.jsonl").exists()


def test_a_queued_ack_returns_the_queued_notice_without_waiting_for_a_reply(env):
    transcript, make = env
    make({"ok": True, "queued": True})
    out = asyncio.run(agy.agy_turn("leg.sock", transcript, "hi", timeout=30, interval=30))
    assert out == agy.QUEUED


@pytest.mark.parametrize("err", [FileNotFoundError(2, "No such file or directory"), ConnectionRefusedError()])
def test_a_conversation_without_a_leg_gets_an_actionable_refusal(tmp_path, monkeypatch, err):
    def boom(*a, **k):
        raise err
    monkeypatch.setattr(agy, "to_leg", boom)
    out = asyncio.run(agy.agy_turn(str(tmp_path / "absent.sock"), str(tmp_path / "t.jsonl"), "hi", timeout=1))
    assert out == agy.NO_LEG and "run_command" in out


def run_leg_with_parent(monkeypatch, tmp_path, parent_pid):
    """Run leg.main pending with AGY_LEG_PARENT_PID set; returns (thread, socket path)."""
    monkeypatch.setattr(leg, "PARENT_POLL_SECS", 0.05)
    monkeypatch.setenv("AGY_LEG_PARENT_PID", str(parent_pid))
    start_leg(monkeypatch, tmp_path, token=None)
    return start_leg.thread,tmp_path / "pend.sock"


def test_leg_exits_and_unlinks_its_endpoint_when_its_agy_parent_is_gone(monkeypatch, tmp_path):
    import subprocess
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    t, sock = run_leg_with_parent(monkeypatch, tmp_path, dead.pid)
    t.join(timeout=5)
    assert not t.is_alive()
    assert not sock.exists()


def test_leg_keeps_serving_while_its_agy_parent_lives(monkeypatch, tmp_path):
    t, sock = run_leg_with_parent(monkeypatch, tmp_path, os.getpid())
    time.sleep(0.3)
    assert t.is_alive() and sock.exists()
    c = socket.socket(socket.AF_UNIX)
    c.connect("pend.sock")  # relative: cwd is tmp_path, whose absolute path can exceed AF_UNIX's limit
    c.sendall(b'{"text": "hi"}\n')
    assert json.loads(c.recv(4096))["queued"] is True
    c.close()


def test_enqueue_blocked_by_a_drain_lands_in_the_replaced_spool(monkeypatch, tmp_path):
    import fcntl
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", "conv-1")
    monkeypatch.setenv("AGY_LEG_SPOOL", str(tmp_path / "q.jsonl"))
    spool = tmp_path / "q.jsonl"
    spool.write_bytes(b"")
    held = open(spool, "rb")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX)  # a drain holding the lock mid-rewrite
    t = threading.Thread(target=leg.enqueue, args=("late",))
    t.start()
    time.sleep(0.1)
    tmp = tmp_path / "q.tmp"
    tmp.write_bytes(b"")
    os.replace(tmp, spool)  # the drain's os.replace
    held.close()
    t.join(timeout=5)
    assert json.loads(spool.read_text())["text"] == "late"


def test_main_wrong_argc_exits_with_usage_and_prints_reply_on_two(monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        agy.main(["only-conv"])
    assert "usage" in str(e.value)

    async def fake_turn(endpoint, transcript, text):
        return f"reply:{endpoint}:{transcript}:{text}"

    monkeypatch.setattr(agy, "agy_turn", fake_turn)
    monkeypatch.setattr(agy, "leg_endpoint", lambda c: f"ep-{c}")
    monkeypatch.setattr(agy, "transcript_path", lambda c: f"tr-{c}")
    assert agy.main(["c1", "hi"]) == 0
    assert capsys.readouterr().out.strip() == "reply:ep-c1:tr-c1:hi"
