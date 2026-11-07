"""Behavior pins for the unimatrix Codex peer adapter against an in-process fake app-server."""
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import struct
import sys
import tempfile
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("unimatrix_peer_codex", BIN / "unimatrix-peer-codex.py")
pcx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pcx)

pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32", reason="asyncio unix-socket streams are unavailable on Windows"),
    pytest.mark.complete,
]

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def frame(opcode, payload, fin=True):
    n = len(payload)
    b0 = (0x80 if fin else 0) | opcode
    if n < 126:
        return struct.pack("!BB", b0, n) + payload
    if n < 65536:
        return struct.pack("!BBH", b0, 126, n) + payload
    return struct.pack("!BBQ", b0, 127, n) + payload


async def read_client_frame(reader):
    b0, b1 = await reader.readexactly(2)
    assert b1 & 0x80, "client frames must be masked"
    n = b1 & 0x7F
    if n == 126:
        (n,) = struct.unpack("!H", await reader.readexactly(2))
    elif n == 127:
        (n,) = struct.unpack("!Q", await reader.readexactly(8))
    mask = await reader.readexactly(4)
    data = bytes(b ^ mask[i % 4] for i, b in enumerate(await reader.readexactly(n)))
    return b0 & 0x0F, data


def make_server(thread_id, calls, big=False, fail_resume=False, hang=False):
    async def handle(reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        key = [l.split(": ", 1)[1] for l in head.split("\r\n") if l.lower().startswith("sec-websocket-key")][0]
        accept = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())

        def send(obj):
            writer.write(frame(0x1, json.dumps(obj).encode()))

        try:
            while True:
                op, data = await read_client_frame(reader)
                if op == 0x8:
                    break
                msg = json.loads(data)
                calls.append(msg)
                m = msg.get("method")
                if m == "initialize":
                    send({"id": msg["id"], "result": {}})
                elif m == "thread/resume":
                    if fail_resume:
                        send({"id": msg["id"], "error": {"message": "no such thread"}})
                    else:
                        send({"id": msg["id"], "result": {}})
                elif m == "turn/start":
                    assert msg["params"]["threadId"] == thread_id
                    send({"id": msg["id"], "result": {}})
                    if hang:
                        await writer.drain()
                        continue
                    writer.write(frame(0x9, b"hi"))  # ping mid-turn must be answered
                    send({"method": "item/agentMessage/delta", "params": {"delta": "par"}})
                    reply = "R" * 70000 if big else "pong:" + msg["params"]["input"][0]["text"]
                    raw = json.dumps({"method": "item/completed", "params": {
                        "item": {"type": "agentMessage", "text": reply}}}).encode()
                    half = len(raw) // 2  # fragmented message
                    writer.write(frame(0x1, raw[:half], fin=False) + frame(0x0, raw[half:]))
                    send({"method": "turn/completed", "params": {}})
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()

    return handle


def run_against(tmp_path, coro_factory, **server_kw):
    calls = []
    # tmp_path overflows the ~104-byte AF_UNIX path limit on macOS, so the socket gets a short dir.
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="cx") as d:
        sock = d + "/s.sock"

        async def go():
            server = await asyncio.start_unix_server(make_server("thr-1", calls, **server_kw), path=sock)
            try:
                return await coro_factory(sock)
            finally:
                server.close()
                await server.wait_closed()

        return asyncio.run(go()), calls


def test_turn_returns_final_agent_message_and_follows_protocol_order(tmp_path):
    out, calls = run_against(tmp_path, lambda s: pcx.codex_turn(s, "thr-1", "ping"))
    assert out == "pong:ping"
    assert [c.get("method") for c in calls] == ["initialize", "initialized", "thread/resume", "turn/start"]
    assert calls[2]["params"] == {"threadId": "thr-1", "excludeTurns": True}


def test_turn_handles_extended_length_frames(tmp_path):
    out, _ = run_against(tmp_path, lambda s: pcx.codex_turn(s, "thr-1", "x" * 70000), big=True)
    assert out == "R" * 70000


def test_error_response_raises(tmp_path):
    with pytest.raises(RuntimeError, match="thread/resume"):
        run_against(tmp_path, lambda s: pcx.codex_turn(s, "thr-1", "x"), fail_resume=True)


def test_silent_server_times_out(tmp_path):
    with pytest.raises(asyncio.TimeoutError):
        run_against(tmp_path, lambda s: pcx.codex_turn(s, "thr-1", "x", timeout=0.3), hang=True)


def fake_codex(tmp_path, monkeypatch, exit_code=0, reply="queued-reply", phase="final_answer",
               prior_turn=None, echo=True):
    """CODEX_HOME in tmp_path with a rollout holding one old reply, and a fake `codex` that logs argv
    and appends a new assistant row to that rollout."""
    home = tmp_path / "home"
    day = home / "sessions" / "2026" / "11" / "04"
    day.mkdir(parents=True)
    rollout = day / "rollout-2026-11-04T00-00-00-thr-1.jsonl"

    def row(role, text, ph=None):
        kind = "input_text" if role == "user" else "output_text"
        p = {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}
        if ph:
            p["phase"] = ph
        return json.dumps({"type": "response_item", "payload": p}) + "\n"

    rollout.write_text(row("assistant", "OLD", "final_answer"))
    log = tmp_path / "argv.log"
    script = tmp_path / "fakecodex.py"
    script.write_text(
        "import sys\n"
        f"open({str(log)!r}, 'w').write(' '.join(sys.argv[1:]))\n"
        + (f"open({str(rollout)!r}, 'a').write({row('assistant', prior_turn, 'final_answer')!r})\n" if prior_turn else "")
        + (f"open({str(rollout)!r}, 'a').write({row('user', 'hello x', None)!r})\n" if echo else "")
        + f"open({str(rollout)!r}, 'a').write({row('assistant', reply, phase)!r})\n"
        f"sys.exit({exit_code})\n")
    shim = tmp_path / "codex"
    shim.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n")
    shim.chmod(0o755)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("CODEX_BIN", str(shim))
    return log


def test_missing_socket_queues_and_reads_reply_from_rollout(tmp_path, monkeypatch):
    log = fake_codex(tmp_path, monkeypatch)
    out = asyncio.run(pcx.codex_turn(str(tmp_path / "nope.sock"), "thr-1", "hello"))
    assert out == "queued-reply"
    assert log.read_text() == "queue --thread thr-1 --message hello"


def test_queue_ignores_rows_before_baseline_and_non_final_phases(tmp_path, monkeypatch):
    fake_codex(tmp_path, monkeypatch, phase="commentary")
    out = asyncio.run(pcx.queue_turn("thr-1", "x", timeout=0.3, interval=0.05))
    assert out == pcx.NO_REPLY


def test_queue_skips_in_flight_turn_before_the_echoed_user_row(tmp_path, monkeypatch):
    fake_codex(tmp_path, monkeypatch, prior_turn="EARLIER-ANSWER")
    out = asyncio.run(pcx.queue_turn("thr-1", "x", timeout=2, interval=0.05))
    assert out == "queued-reply"


def test_queue_without_echoed_user_row_yields_no_reply(tmp_path, monkeypatch):
    fake_codex(tmp_path, monkeypatch, echo=False)
    out = asyncio.run(pcx.queue_turn("thr-1", "x", timeout=0.3, interval=0.05))
    assert out == pcx.NO_REPLY


def test_queue_failure_raises(tmp_path, monkeypatch):
    fake_codex(tmp_path, monkeypatch, exit_code=2)
    with pytest.raises(RuntimeError, match="codex queue failed"):
        asyncio.run(pcx.codex_turn(str(tmp_path / "nope.sock"), "thr-1", "x"))


def test_refused_upgrade_does_not_fall_back_to_queue(tmp_path, monkeypatch):
    log = fake_codex(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="thread/resume"):
        run_against(tmp_path, lambda s: pcx.codex_turn(s, "thr-1", "x"), fail_resume=True)
    assert not log.exists()


def test_tui_held_thread_queues_even_when_the_socket_connects(tmp_path, monkeypatch):
    log = fake_codex(tmp_path, monkeypatch)
    out, calls = run_against(tmp_path, lambda s: pcx.codex_turn(s, "thr-1", "hello", tui_held=True))
    assert out == "queued-reply" and calls == []
    assert log.read_text() == "queue --thread thr-1 --message hello"


def test_codex_home_defaults_to_home_dot_codex(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert pcx.codex_home() == Path.home() / ".codex"


def test_socket_path_env_override_and_default(monkeypatch):
    monkeypatch.setenv("CODEX_APP_SERVER_SOCK", "/x/y.sock")
    assert pcx.socket_path() == "/x/y.sock"
    monkeypatch.delenv("CODEX_APP_SERVER_SOCK")
    assert pcx.socket_path() == os.path.expanduser("~/.codex/app-server-control/app-server-control.sock")


def test_main_wrong_argc_exits_with_usage_and_prints_reply_on_two(monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        pcx.main(["only-thread"])
    assert "usage" in str(e.value)

    async def fake_turn(sock, thread, text):
        return f"reply:{thread}:{text}"

    monkeypatch.setattr(pcx, "codex_turn", fake_turn)
    monkeypatch.setattr(pcx, "socket_path", lambda: "/x.sock")
    assert pcx.main(["t1", "hello"]) == 0
    assert capsys.readouterr().out.strip() == "reply:t1:hello"
