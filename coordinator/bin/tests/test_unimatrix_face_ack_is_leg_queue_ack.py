"""A relayed frame is acknowledged to its sender only with the router's own answer, which for a pending agy leg is the leg queue's.

Contract: for every `type: user` frame the face writes exactly one JSON line, the router's answer verbatim
(or ok:false when the router is unreachable); the relay reads that line before it replies ok.
"""
import asyncio
import importlib.util
import io
import json
import os
import socket
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

BIN = Path(__file__).resolve().parents[1]
NAME = "agy-session-5d60623569-on-mac-mini"
CONV = "2a49865c-2678-4d7f-967e-cc9f86f19b99"


def _load(mod_name, file_name):
    spec = importlib.util.spec_from_file_location(mod_name, BIN / file_name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


router = _load("face_ack_router", "unimatrix-peer-router.py")


def user_frame(body="please look at this"):
    return {"type": "user", "from": "uds:/far/sender.sock", "message": {"content": body}}


class FakeReader:
    def __init__(self, raw):
        self.raw = raw
        self.auth_token = "tok"

    async def read(self):
        return self.raw


class FakeWriter:
    def __init__(self, on_write=None):
        self.lines = []
        self.closed = False
        self.on_write = on_write

    def write(self, data):
        if self.on_write:
            self.on_write()
        self.lines.append(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True


def drive_face(tmp_path, monkeypatch, request_line, raw, on_write=None):
    """Run the real run_face on_conn once over `raw`; return the FakeWriter it answered on."""
    captured = {}

    async def fake_start(handler, endpoint):
        captured["handler"] = handler
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(router, "start_private_server", fake_start)
    monkeypatch.setattr(router, "request_line", request_line)
    monkeypatch.setattr(router.Registration, "accepts", lambda self, token: True)

    async def go():
        task = asyncio.ensure_future(router.run_face(
            NAME, str(tmp_path / "face.sock"), "router-ctl", str(tmp_path / "sessions"), leg_mode="pending"))
        for _ in range(500):
            if "handler" in captured:
                break
            await asyncio.sleep(0.01)
        writer = FakeWriter(on_write)
        await captured["handler"](FakeReader(raw), writer)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return writer

    return asyncio.run(go())


def frame_bytes(body="please look at this"):
    return (json.dumps(user_frame(body)) + "\n").encode("utf-8")


def answers(writer):
    return [json.loads(line) for line in b"".join(writer.lines).splitlines()]


def test_face_writes_the_router_ok_for_a_frame_the_router_took(tmp_path, monkeypatch):
    writer = drive_face(tmp_path, monkeypatch, lambda ep, req, timeout=15, **k: {"ok": True}, frame_bytes())
    assert answers(writer) == [{"ok": True}]


def test_face_writes_the_router_refusal_verbatim(tmp_path, monkeypatch):
    refusal = {"ok": False, "error": "no leg queue for x: the pending leg is gone; message not delivered"}
    writer = drive_face(tmp_path, monkeypatch, lambda ep, req, timeout=15, **k: dict(refusal), frame_bytes())
    assert answers(writer) == [refusal]


def test_face_answers_ok_false_when_the_router_is_unreachable(tmp_path, monkeypatch):
    def unreachable(ep, req, timeout=15, **k):
        raise ConnectionRefusedError("router gone")

    (answer,) = answers(drive_face(tmp_path, monkeypatch, unreachable, frame_bytes()))
    assert answer["ok"] is False and "router unreachable" in answer["error"]


def test_face_writes_exactly_one_line_per_user_frame(tmp_path, monkeypatch):
    writer = drive_face(tmp_path, monkeypatch, lambda ep, req, timeout=15, **k: {"ok": True}, frame_bytes())
    assert len(b"".join(writer.lines).splitlines()) == 1


def test_a_probe_connection_with_no_bytes_is_answered_with_nothing(tmp_path, monkeypatch):
    writer = drive_face(tmp_path, monkeypatch, lambda ep, req, timeout=15, **k: {"ok": True}, b"")
    assert writer.lines == []


# The face's answer through a real router and a real pending leg ---------------------------------------


@pytest.fixture
def leg(tmp_path, monkeypatch):
    monkeypatch.setenv("AGY_LEG_LOG", str(tmp_path / "legs" / f"{CONV}.log"))
    monkeypatch.setenv("AGY_LEG_SPOOL", str(tmp_path / "legs" / f"{CONV}.queue.jsonl"))
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", CONV)
    monkeypatch.delenv("ANTIGRAVITY_CSRF_TOKEN", raising=False)
    (tmp_path / "legs").mkdir()
    mod = _load("face_ack_leg", "unimatrix-agy-leg.py")
    monkeypatch.setattr(mod, "_PENDING", True)
    return mod


def chain(tmp_path, monkeypatch, leg_mod, sender, body, on_write=None):
    """face -> real Router._dispatch -> real agy adapter turn -> `sender` (the leg connection) -> face answer."""
    from _unimatrix.adapters import agy

    async def adapter(peer, text):
        return await agy.agy_turn("leg-endpoint", tmp_path / "transcript.jsonl", text,
                                  sender=sender, wait_reply=False)

    r = router.Router([], sessions_dir=tmp_path, state_dir=tmp_path / "state", adapters={"agy": adapter})
    r.pending_legs = {NAME: {"name": NAME, "target": "5d60623569"}}

    def request_line(ep, req, timeout=15, **k):
        return asyncio.run(r._dispatch(req))

    return drive_face(tmp_path, monkeypatch, request_line, frame_bytes(body), on_write)


def through_leg_respond(leg_mod):
    def sender(leg_sock, text):
        return json.loads(leg_mod.respond(json.dumps({"text": text}).encode()))
    return sender


def queue_text(leg_mod):
    path = leg_mod.spool_path()
    if not os.path.exists(path):
        return ""
    return Path(path).read_text(encoding="utf-8")


def test_a_leg_that_queues_is_acked_ok_only_once_its_queue_file_holds_the_text(tmp_path, monkeypatch, leg):
    seen_at_ack = []
    writer = chain(tmp_path, monkeypatch, leg, through_leg_respond(leg), "unique-frame-text-7f3a",
                   on_write=lambda: seen_at_ack.append(queue_text(leg)))
    assert answers(writer) == [{"ok": True}]
    assert len(seen_at_ack) == 1 and "unique-frame-text-7f3a" in seen_at_ack[0]


def test_a_leg_whose_endpoint_is_dead_is_acked_ok_false_naming_the_leg_endpoint(tmp_path, monkeypatch, leg):
    def dead(leg_sock, text):
        raise FileNotFoundError(leg_sock)

    (answer,) = answers(chain(tmp_path, monkeypatch, leg, dead, "to-a-dead-leg"))
    assert answer["ok"] is False and "no leg endpoint" in answer["error"]
    assert queue_text(leg) == ""


def test_a_leg_whose_endpoint_refuses_the_connection_is_acked_ok_false(tmp_path, monkeypatch, leg):
    def refused(leg_sock, text):
        raise ConnectionRefusedError(leg_sock)

    (answer,) = answers(chain(tmp_path, monkeypatch, leg, refused, "to-a-refusing-leg"))
    assert answer["ok"] is False and "no leg endpoint" in answer["error"]


def test_a_leg_whose_spool_is_full_is_acked_ok_false(tmp_path, monkeypatch, leg):
    monkeypatch.setattr(leg, "SPOOL_MAX_BYTES", 8)
    Path(leg.spool_path()).write_text("x" * 16, encoding="utf-8")
    (answer,) = answers(chain(tmp_path, monkeypatch, leg, through_leg_respond(leg), "to-a-full-spool"))
    assert answer["ok"] is False and "spool" in answer["error"]
    assert "to-a-full-spool" not in queue_text(leg)


# The relay reads the face ack before it replies ----------------------------------------------------------

LINE = b'{"from":"uds:/mini-a","message":"relayed one"}\n'
unix_only = pytest.mark.skipif(sys.platform == "win32", reason="unix-socket branch of the relay write")


@pytest.fixture
def relay(tmp_path, monkeypatch):
    socks = tmp_path / "s"
    socks.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setenv("UNIMATRIX_LOOPBACK_SOCKS", str(socks))
    mod = _load("face_ack_relay", "unimatrix-relay.py")
    monkeypatch.setattr(mod, "SESS", str(tmp_path / "sessions"))
    monkeypatch.setattr(mod, "local_reply_address", lambda line: line)
    monkeypatch.setattr(mod, "_own_box", lambda: "machine-a")
    (tmp_path / "sessions").mkdir()
    mod.socks_dir = socks
    return mod


def register(relay, tmp_path, sock_path, router_face):
    """Write the registry row (+ key) the relay resolves pid -> endpoint through."""
    pid = os.getpid()
    row = {"pid": pid, "name": NAME, "sessionId": "sid-1", "messagingSocketPath": str(sock_path)}
    if router_face:
        row["unimatrixRouterPid"] = pid
    (tmp_path / "sessions" / f"{pid}.json").write_text(json.dumps(row), encoding="utf-8")
    (tmp_path / "sessions" / f"{pid}.key").write_text("k", encoding="utf-8")
    return pid


class FaceServer:
    """A unix-socket endpoint that reads the frame to EOF (as the face's on_conn does), then answers `answer`."""

    def __init__(self, path, answer, read_to_eof=True):
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(str(path))
        self.srv.listen(1)
        self.answer = answer
        self.read_to_eof = read_to_eof
        self.got = b""
        self.acked = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        conn, _ = self.srv.accept()
        try:
            buf = b""
            while True:
                if not self.read_to_eof and b"\n" in buf:
                    break
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            self.got = buf
            if self.answer is not None:
                try:
                    conn.sendall(self.answer)
                except OSError:
                    return
            self.acked.set()
            if not self.read_to_eof:
                self.release.wait(5)
        finally:
            conn.close()

    def close(self):
        self.release.set()
        self.srv.close()


def relay_batch(relay, monkeypatch, capsys, pid):
    monkeypatch.setattr(relay.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(LINE)))
    relay.send_batch(pid)
    return [json.loads(x) for x in capsys.readouterr().out.splitlines()]


def serve_face(relay, tmp_path, answer, router_face=True, read_to_eof=True):
    sock = relay.socks_dir / "face.sock"
    if len(str(sock)) > 100:
        pytest.skip("tmp_path too long for an AF_UNIX path")
    server = FaceServer(sock, answer, read_to_eof)
    return server, register(relay, tmp_path, sock, router_face)


@unix_only
def test_relay_replies_ok_false_when_the_face_acks_ok_false(relay, tmp_path, monkeypatch, capsys):
    server, pid = serve_face(relay, tmp_path, b'{"ok": false, "error": "no leg endpoint for the pending leg"}\n')
    try:
        (reply,) = relay_batch(relay, monkeypatch, capsys, pid)
    finally:
        server.close()
    assert reply["ok"] is False and "no leg endpoint" in reply["error"]
    assert server.got == LINE


@unix_only
def test_relay_replies_ok_only_after_the_face_acked_ok(relay, tmp_path, monkeypatch, capsys):
    server, pid = serve_face(relay, tmp_path, b'{"ok": true}\n')
    try:
        (reply,) = relay_batch(relay, monkeypatch, capsys, pid)
        acked_before_reply = server.acked.is_set()
    finally:
        server.close()
    assert reply["ok"] is True and acked_before_reply


@unix_only
def test_relay_replies_ok_false_when_the_face_closes_without_an_ack_line(relay, tmp_path, monkeypatch, capsys):
    server, pid = serve_face(relay, tmp_path, None)
    try:
        (reply,) = relay_batch(relay, monkeypatch, capsys, pid)
    finally:
        server.close()
    assert reply["ok"] is False and reply.get("error")


@unix_only
def test_relay_does_not_wait_for_an_ack_from_a_session_that_is_not_a_router_face(relay, tmp_path, monkeypatch, capsys):
    server, pid = serve_face(relay, tmp_path, None, router_face=False, read_to_eof=False)
    try:
        (reply,) = relay_batch(relay, monkeypatch, capsys, pid)
    finally:
        server.close()
    assert reply["ok"] is True
    assert server.got == LINE


PIPE = r"\\.\pipe\face-ack-target"


def fake_pipe(monkeypatch, relay, ack_bytes):
    """A byte-mode pipe handle: the write is recorded, one read hands back `ack_bytes`."""
    wrote = []
    fake = SimpleNamespace(
        GENERIC_READ=1, GENERIC_WRITE=2, OPEN_EXISTING=3,
        CreateFile=lambda *a: "handle", CloseHandle=lambda h: None,
        WriteFile=lambda h, line: wrote.append(line),
        FlushFileBuffers=lambda h: None,
        ReadFile=lambda h, n, *a: (0, ack_bytes))
    monkeypatch.setitem(sys.modules, "win32file", fake)
    monkeypatch.setattr(relay, "endpoint_kind", lambda platform=None: "pipe")
    monkeypatch.setattr(relay, "load_roster", lambda: SimpleNamespace(
        authenticated_data=lambda p, line, s: line, session_identity=lambda d: {}))
    monkeypatch.setattr(relay, "_pipe_server_pid", lambda h: None)
    monkeypatch.setattr(relay, "_flush_bounded", lambda flush, deadline=None: None)
    row = {"pid": 7, "name": NAME, "sessionId": "sid-1", "messagingSocketPath": PIPE, "unimatrixRouterPid": 7}
    monkeypatch.setattr(relay, "entries", lambda: iter([(7, row, "7.key")]))
    return wrote


def test_pipe_relay_replies_ok_false_when_the_face_acks_ok_false(relay, monkeypatch, capsys):
    wrote = fake_pipe(monkeypatch, relay, b'{"ok": false, "error": "no leg endpoint"}\n')
    (reply,) = relay_batch(relay, monkeypatch, capsys, 7)
    assert wrote == [LINE]
    assert reply["ok"] is False and "no leg endpoint" in reply["error"]


def test_pipe_relay_replies_ok_when_the_face_acks_ok(relay, monkeypatch, capsys):
    fake_pipe(monkeypatch, relay, b'{"ok": true}\n')
    (reply,) = relay_batch(relay, monkeypatch, capsys, 7)
    assert reply["ok"] is True


def test_pipe_relay_replies_ok_false_when_the_face_gives_no_ack_line(relay, monkeypatch, capsys):
    fake_pipe(monkeypatch, relay, b"")
    (reply,) = relay_batch(relay, monkeypatch, capsys, 7)
    assert reply["ok"] is False and reply.get("error")
