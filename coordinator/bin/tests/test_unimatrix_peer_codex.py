"""Behavior pins for the unimatrix Codex peer adapter against an in-process fake app-server."""
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import struct
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("unimatrix_peer_codex", BIN / "unimatrix-peer-codex.py")
pcx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pcx)

pytestmark = [pytest.mark.complete]
posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="asyncio unix-socket streams and sh shims are unavailable on Windows")

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


@posix_only
def test_turn_returns_final_agent_message_and_follows_protocol_order(tmp_path):
    out, calls = run_against(tmp_path, lambda s: pcx.codex_turn(s, "thr-1", "ping"))
    assert out == "pong:ping"
    assert [c.get("method") for c in calls] == ["initialize", "initialized", "thread/resume", "turn/start"]
    assert calls[2]["params"] == {"threadId": "thr-1", "excludeTurns": True}


@posix_only
def test_turn_handles_extended_length_frames(tmp_path):
    out, _ = run_against(tmp_path, lambda s: pcx.codex_turn(s, "thr-1", "x" * 70000), big=True)
    assert out == "R" * 70000


@posix_only
def test_error_response_raises(tmp_path):
    with pytest.raises(RuntimeError, match="thread/resume"):
        run_against(tmp_path, lambda s: pcx.codex_turn(s, "thr-1", "x"), fail_resume=True)


@posix_only
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


@posix_only
def test_missing_socket_queues_and_reads_reply_from_rollout(tmp_path, monkeypatch):
    log = fake_codex(tmp_path, monkeypatch)
    out = asyncio.run(pcx.codex_turn(str(tmp_path / "nope.sock"), "thr-1", "hello"))
    assert out == "queued-reply"
    assert log.read_text() == "--remote unix:// queue --thread thr-1 --message hello"


@posix_only
def test_queue_ignores_rows_before_baseline_and_non_final_phases(tmp_path, monkeypatch):
    fake_codex(tmp_path, monkeypatch, phase="commentary")
    out = asyncio.run(pcx.queue_turn("thr-1", "x", timeout=0.3, interval=0.05))
    assert out == pcx.NO_REPLY


@posix_only
def test_queue_skips_in_flight_turn_before_the_echoed_user_row(tmp_path, monkeypatch):
    fake_codex(tmp_path, monkeypatch, prior_turn="EARLIER-ANSWER")
    out = asyncio.run(pcx.queue_turn("thr-1", "x", timeout=2, interval=0.05))
    assert out == "queued-reply"


@posix_only
def test_queue_without_echoed_user_row_yields_no_reply(tmp_path, monkeypatch):
    fake_codex(tmp_path, monkeypatch, echo=False)
    out = asyncio.run(pcx.queue_turn("thr-1", "x", timeout=0.3, interval=0.05))
    assert out == pcx.NO_REPLY


@posix_only
def test_queue_failure_raises_when_a_tui_holds_the_thread(tmp_path, monkeypatch):
    fake_codex(tmp_path, monkeypatch, exit_code=2)
    with pytest.raises(RuntimeError, match="codex queue failed"):
        asyncio.run(pcx.codex_turn(str(tmp_path / "nope.sock"), "thr-1", "x", tui_held=True))


@posix_only
def test_refused_upgrade_does_not_fall_back_to_queue(tmp_path, monkeypatch):
    log = fake_codex(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="thread/resume"):
        run_against(tmp_path, lambda s: pcx.codex_turn(s, "thr-1", "x"), fail_resume=True)
    assert not log.exists()


@posix_only
def test_tui_held_thread_queues_even_when_the_socket_connects(tmp_path, monkeypatch):
    log = fake_codex(tmp_path, monkeypatch)
    out, calls = run_against(tmp_path, lambda s: pcx.codex_turn(s, "thr-1", "hello", tui_held=True))
    assert out == "queued-reply" and calls == []
    assert log.read_text() == "--remote unix:// queue --thread thr-1 --message hello"


def stub_codex_run(monkeypatch, tmp_path, queue_rc=1, exec_rc=0, reply="exec-reply", with_rollout=False):
    """Replace subprocess.run: `codex queue` exits queue_rc; `codex exec` writes `reply` to its -o file."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_BIN", "fake-codex")
    if with_rollout:
        day = tmp_path / "home" / "sessions" / "2026" / "11" / "21"
        day.mkdir(parents=True)
        (day / "rollout-2026-11-21T00-00-00-thr-1.jsonl").write_text("{}\n")
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        if "queue" in argv:
            return subprocess.CompletedProcess(argv, queue_rc, "", "no app-server")
        if "-o" in argv:
            Path(argv[argv.index("-o") + 1]).write_text(reply + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, exec_rc, "", "boom" if exec_rc else "")

    monkeypatch.setattr(pcx.subprocess, "run", fake_run)
    return calls


def test_no_live_app_server_falls_back_to_one_headless_exec(tmp_path, monkeypatch):
    calls = stub_codex_run(monkeypatch, tmp_path)
    monkeypatch.setattr(pcx.sys, "platform", "linux")
    out = asyncio.run(pcx.codex_turn(str(tmp_path / "nope.sock"), "thr-1", "hello"))
    assert out == "exec-reply"
    execs = [(a, k) for a, k in calls if "exec" in a]
    assert len(execs) == 1
    argv, kw = execs[0]
    assert argv[0] == "fake-codex" and argv[1:3] == ["exec", "--skip-git-repo-check"]
    assert argv[-1] == "-" and kw["input"] == "hello"
    assert kw["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_headless_exec_resumes_a_thread_with_a_rollout_else_runs_fresh(tmp_path, monkeypatch):
    calls = stub_codex_run(monkeypatch, tmp_path, with_rollout=True)
    asyncio.run(pcx.exec_turn("thr-1", "hi"))
    assert calls[0][0][1:3] == ["exec", "resume"] and calls[0][0][-2:] == ["thr-1", "-"]
    calls = stub_codex_run(monkeypatch, tmp_path / "other")
    asyncio.run(pcx.exec_turn("thr-2", "hi"))
    assert "resume" not in calls[0][0] and "thr-2" not in calls[0][0]


def test_headless_exec_failure_raises_and_timeout_yields_no_reply(tmp_path, monkeypatch):
    stub_codex_run(monkeypatch, tmp_path, exec_rc=3)
    with pytest.raises(RuntimeError, match=r"codex exec failed \(3\): boom"):
        asyncio.run(pcx.exec_turn("thr-1", "x"))

    def timing_out(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(pcx.subprocess, "run", timing_out)
    assert asyncio.run(pcx.exec_turn("thr-1", "x", timeout=1)) == pcx.NO_REPLY


def test_a_live_queue_is_not_replaced_by_exec_and_a_tui_held_thread_never_execs(tmp_path, monkeypatch):
    calls = stub_codex_run(monkeypatch, tmp_path, queue_rc=0)
    monkeypatch.setattr(pcx.sys, "platform", "win32")
    monkeypatch.setattr(pcx, "RolloutTail", lambda thread_id: (_ for _ in ()).throw(ValueError("no baseline")))
    assert asyncio.run(pcx.codex_turn("s", "thr-1", "x")) == pcx.NO_REPLY
    assert [a for a, _ in calls if "exec" in a] == []
    stub_codex_run(monkeypatch, tmp_path)
    with pytest.raises(pcx.QueueFailed):
        asyncio.run(pcx.codex_turn("s", "thr-1", "x", tui_held=True))


def test_exec_env_strips_windowsapps_only_on_windows(monkeypatch):
    monkeypatch.setenv("PATH", os.pathsep.join([os.path.join("stub", "Microsoft", "WindowsApps"), "keep"]))
    path = pcx.exec_env()["PATH"]
    if os.name == "nt":
        assert "windowsapps" not in path.lower() and "keep" in path
    else:
        assert "WindowsApps" in path


def test_codex_home_defaults_to_home_dot_codex(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert pcx.codex_home() == Path.home() / ".codex"


def _rollout(home, thread_id, first_row):
    day = home / "sessions" / "2026" / "11" / "08"
    day.mkdir(parents=True, exist_ok=True)
    (day / f"rollout-2026-11-08T00-00-00-{thread_id}.jsonl").write_text(
        (first_row if isinstance(first_row, str) else json.dumps(first_row)) + "\n{}\n")


def test_thread_addressable_needs_a_rollout_whose_source_is_not_subagent(tmp_path):
    ids = {n: f"01a1b600-0000-7000-8000-0000000000{i:02x}" for i, n in enumerate(
        ("cli", "sub", "str", "none", "torn", "other"))}
    _rollout(tmp_path, ids["cli"], {"type": "session_meta", "payload": {"source": "cli"}})
    _rollout(tmp_path, ids["sub"], {"type": "session_meta", "payload": {"source": {"subagent": {"x": 1}}}})
    _rollout(tmp_path, ids["str"], {"type": "session_meta", "payload": {"source": "subagent"}})
    _rollout(tmp_path, ids["torn"], '{"type": "session_m')
    _rollout(tmp_path, ids["other"], {"type": "event_msg", "payload": {}})
    index = pcx.rollout_index(tmp_path)
    got = {n: pcx.thread_addressable(t, tmp_path, index) for n, t in ids.items()}
    assert got == {"cli": True, "sub": False, "str": False, "none": False, "torn": False, "other": True}
    assert pcx.thread_addressable(ids["cli"], tmp_path) is True  # single-id caller, no index
    assert pcx.thread_addressable(ids["sub"], tmp_path) is False
    assert pcx.rollout_path(ids["cli"], tmp_path) == index[ids["cli"]]


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


class FakeDaemon:
    """Stands in for WsClient over the daemon socket: records calls, answers like the app-server daemon."""
    instances = []

    def __init__(self, via):
        self.via, self.calls, self.sent, self.closed = via, [], [], False
        self.pending = ['{"method":"item/agentMessage/delta","params":{}}', '{"method":"turn/completed","params":{}}']
        FakeDaemon.instances.append(self)

    @classmethod
    async def connect(cls, sock):
        return cls(("connect", sock))

    proc = None

    @classmethod
    async def spawn(cls, cwd=None):
        d = cls(("spawn", cwd))
        d.proc = types.SimpleNamespace(pid=4242)
        return d

    async def call(self, method, params, timeout=0):
        self.calls.append((method, params))
        return {"thread": {"id": "thread-from-daemon"}} if method == "thread/start" else {}

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        return self.pending.pop(0)

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("platform,via", [("linux", ("connect", "/d.sock")), ("win32", ("spawn", "/work"))])
def test_start_thread_runs_initialize_thread_start_and_priming_turn_on_the_daemon_socket(monkeypatch, platform, via):
    FakeDaemon.instances = []
    monkeypatch.setattr(pcx, "WsClient", FakeDaemon)
    monkeypatch.setattr(pcx, "StdioClient", FakeDaemon)
    monkeypatch.setattr(pcx.sys, "platform", platform)
    faces = []
    monkeypatch.setattr(pcx, "register_face", lambda tid, pid: faces.append((tid, pid)))
    assert asyncio.run(pcx.start_thread("/d.sock", cwd="/work")) == "thread-from-daemon"
    assert faces == ([("thread-from-daemon", 4242)] if platform == "win32" else [])
    d = FakeDaemon.instances[0]
    assert d.via == via and d.closed and d.pending == []
    assert [c[0] for c in d.calls] == ["initialize", "thread/start", "turn/start"]
    assert d.calls[1][1] == {"cwd": "/work"}
    assert d.calls[2][1]["threadId"] == "thread-from-daemon"
    assert d.sent == [{"method": "initialized"}]


def test_start_thread_refuses_when_the_daemon_is_unreachable(monkeypatch):
    class Gone(FakeDaemon):
        @classmethod
        async def connect(cls, sock):
            raise ConnectionError("no daemon")

    monkeypatch.setattr(pcx, "WsClient", Gone)
    monkeypatch.setattr(pcx.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="not reachable"):
        asyncio.run(pcx.start_thread("/d.sock"))


def test_main_start_face_prints_the_thread_id(monkeypatch, capsys):
    seen = {}

    async def fake_start(sock, cwd=None, on_ready=None, hold=False):
        seen.update(sock=sock, cwd=cwd, hold=hold)
        on_ready("tid-1")

    monkeypatch.setattr(pcx, "start_thread", fake_start)
    monkeypatch.setattr(pcx, "socket_path", lambda: "/x.sock")
    assert pcx.main(["--start-face", "/w"]) == 0
    assert capsys.readouterr().out.strip() == "tid-1" and seen == {"sock": "/x.sock", "cwd": "/w", "hold": True}
    with pytest.raises(SystemExit):
        pcx.main(["--start-face", "a", "b"])


def test_ws_call_returns_the_matching_result_and_raises_on_error():
    class Scripted(pcx.WsClient):
        def __init__(self, inbound):
            super().__init__(None, None)
            self.inbound, self.out = list(inbound), []

        async def send(self, text):
            self.out.append(json.loads(text))

        async def recv(self):
            return self.inbound.pop(0)

    ok = Scripted(['{"method":"note"}', '{"id":1,"result":{"a":1}}'])
    assert asyncio.run(ok.call("m", {"x": 1})) == {"a": 1} and ok.out[0]["method"] == "m"
    bad = Scripted(['{"id":1,"error":{"code":-1}}'])
    with pytest.raises(RuntimeError, match="m:"):
        asyncio.run(bad.call("m", {}))


class StdioDaemon(FakeDaemon):
    """FakeDaemon whose stream carries an agent reply, for stdio_turn."""

    def __init__(self, via):
        super().__init__(via)
        self.pending = ['{"method":"item/agentMessage/delta","params":{"delta":"hi "}}',
                        '{"method":"item/completed","params":{"item":{"type":"agentMessage","text":"hi there"}}}',
                        '{"method":"turn/completed","params":{}}']


def test_windows_headless_turn_runs_on_a_stdio_child_that_registers_a_face_not_codex_exec(tmp_path, monkeypatch):
    calls = stub_codex_run(monkeypatch, tmp_path)
    FakeDaemon.instances = []
    monkeypatch.setattr(pcx, "StdioClient", StdioDaemon)
    monkeypatch.setattr(pcx.sys, "platform", "win32")
    faces = []
    monkeypatch.setattr(pcx, "register_face", lambda tid, pid: faces.append((tid, pid)))
    assert asyncio.run(pcx.codex_turn(str(tmp_path / "nope.sock"), "thr-1", "hello")) == "hi there"
    assert [a for a, _ in calls if "exec" in a] == []
    d = FakeDaemon.instances[0]
    assert faces == [("thread-from-daemon", 4242)]
    assert d.via == ("spawn", None) and d.closed
    assert [c[0] for c in d.calls] == ["initialize", "thread/start", "turn/start"]
    assert d.calls[2][1]["threadId"] == "thread-from-daemon"
    assert d.calls[2][1]["input"] == [{"type": "text", "text": "hello"}]


def test_windows_headless_turn_resumes_a_thread_that_has_a_rollout(tmp_path, monkeypatch):
    stub_codex_run(monkeypatch, tmp_path, with_rollout=True)
    FakeDaemon.instances = []
    monkeypatch.setattr(pcx, "StdioClient", StdioDaemon)
    monkeypatch.setattr(pcx.sys, "platform", "win32")
    asyncio.run(pcx.codex_turn(str(tmp_path / "nope.sock"), "thr-1", "hello"))
    d = FakeDaemon.instances[0]
    assert [c[0] for c in d.calls] == ["initialize", "thread/resume", "turn/start"]
    assert d.calls[1][1]["threadId"] == "thr-1" and d.calls[2][1]["threadId"] == "thr-1"


def test_stdio_client_frames_newline_delimited_json_and_closes_the_child():
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(b'\n{"id":1,"result":{"a":1}}\n')

        class W:
            buf = b""
            def write(self, b): W.buf += b
            async def drain(self): pass
            def close(self): pass

        class P:
            returncode = None
            killed = False
            def kill(self): P.killed = True
            async def wait(self): pass

        c = pcx.StdioClient(reader, W())
        c.proc = P()
        assert await c.call("m", {"x": 1}) == {"a": 1}
        assert json.loads(W.buf) == {"id": 1, "method": "m", "params": {"x": 1}} and W.buf.endswith(b"\n")
        reader.feed_eof()
        with pytest.raises(ConnectionError):
            await c.recv()
        await c.close()
        assert P.killed

    asyncio.run(run())


UUID = "0d6c7a52-2f0e-4c53-9f8d-3a1b5e7c9d10"


def bind_address(registry, thread=UUID):
    """Write the engine's Codex address binding for `thread` into `registry`."""
    registry.mkdir(parents=True, exist_ok=True)
    (registry / f"{thread}.json").write_text(json.dumps({
        "schema_version": 1, "provider": "codex", "session_id": thread, "address_version": 1,
        "name": "machine-a-codex-face-1", "box": pcx.socket.gethostname()}), encoding="utf-8")


def test_register_face_writes_a_peers_row_with_the_address_and_live_holder(tmp_path):
    bind_address(tmp_path / "reg")
    peers = tmp_path / "peers.json"
    assert pcx.register_face(UUID, os.getpid(), peers_file=peers, registry_dir=tmp_path / "reg") == "added"
    row = json.loads(peers.read_text(encoding="utf-8"))[0]
    assert (row["name"], row["vendor"], row["target"], row["codexPid"]) == ("machine-a-codex-face-1", "codex", UUID, os.getpid())
    assert pcx.register_face(UUID, os.getpid(), peers_file=peers, registry_dir=tmp_path / "reg") == "present"


def test_register_face_refuses_a_thread_the_engine_has_not_bound(tmp_path):
    peers = tmp_path / "peers.json"
    with pytest.raises(RuntimeError, match="unbound"):
        pcx.register_face(UUID, os.getpid(), peers_file=peers, registry_dir=tmp_path / "reg")
    assert not peers.exists()


def test_windows_headless_turn_still_replies_when_the_thread_is_unbound(tmp_path, monkeypatch):
    stub_codex_run(monkeypatch, tmp_path)
    monkeypatch.setattr(pcx, "StdioClient", StdioDaemon)
    monkeypatch.setattr(pcx.sys, "platform", "win32")

    def unbound(tid, pid):
        raise RuntimeError("unbound")

    monkeypatch.setattr(pcx, "register_face", unbound)
    assert asyncio.run(pcx.codex_turn(str(tmp_path / "nope.sock"), "thr-1", "hello")) == "hi there"
