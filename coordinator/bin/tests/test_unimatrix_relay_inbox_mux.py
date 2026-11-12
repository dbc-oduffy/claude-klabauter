"""The far relay's `inbox-mux` mode: one process hosts every mirror name of a near box."""
import importlib.util
import io
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("relay_inbox_mux_t", BIN / "unimatrix-relay.py")
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="AF_UNIX required")

NAMES = [f"face-{i}" for i in range(6)]


class FakeProc:
    stdout = "Thu Nov 12 13:00:00 2026"


class Out:
    def __init__(self):
        self.lines, self.lock = [], threading.Lock()

    def write(self, b):
        with self.lock:
            self.lines += [json.loads(x) for x in b.splitlines() if x]

    def flush(self):
        pass


@pytest.fixture
def host(tmp_path, monkeypatch):
    sess, socks = tmp_path / "sess", tmp_path / "s"
    sess.mkdir()
    monkeypatch.setattr(relay, "SESS", str(sess))
    monkeypatch.setattr(relay, "SOCKS", str(socks))
    monkeypatch.setattr(relay, "endpoint_kind", lambda platform=None: "unix")
    monkeypatch.setattr(relay, "client_address", lambda: "10.0.0.9")
    monkeypatch.setattr(relay.subprocess, "run", lambda *a, **k: FakeProc())

    class Helpers:
        @staticmethod
        def local_peer_key(body, ps, domain):
            return f"{body}|{ps}|{domain}"

        @staticmethod
        def write_peer_key(path, value):
            Path(path).write_text(value, encoding="utf-8")

    monkeypatch.setattr(relay, "load_roster", lambda: Helpers)
    monkeypatch.setattr(relay, "check_name_free", lambda name, client="", own_pid=None: [])
    return sess, socks


def start_mux():
    r, w = os.pipe()
    out = Out()
    t = threading.Thread(target=relay.serve_mux, args=(r, out), daemon=True)
    t.start()
    return w, t, out


def send(w, **frame):
    os.write(w, json.dumps(frame).encode() + b"\n")


def wait(pred, secs=5):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_parse_mode_accepts_inbox_mux_and_nothing_wider():
    assert relay.parse_mode("inbox-mux") == ("inbox-mux", None)
    assert relay.parse_mode("inbox-mux extra") is None
    assert relay.parse_mode("inbox face-0") == ("inbox", "face-0")


def test_one_process_registers_six_names_and_releases_all_on_stdin_eof(host):
    sess, socks = host
    w, t, out = start_mux()
    for n in NAMES:
        send(w, op="add", name=n, entry={"name": n, "cwd": "/x"}, key="KEYBODY")
    assert wait(lambda: len(list(sess.glob("*.json"))) == 6)
    entries = [json.loads(p.read_text()) for p in sess.glob("*.json")]
    assert {e["pid"] for e in entries} == {os.getpid()}
    assert sorted(e["name"] for e in entries) == sorted(NAMES)
    assert len(list(socks.glob("*.sock"))) == 6 and len(list(sess.glob("*.key"))) == 6
    os.close(w)
    t.join(5)
    assert not t.is_alive()
    assert not list(sess.iterdir()) and not list(socks.iterdir())


def test_a_message_to_a_names_socket_is_wrapped_with_that_name(host):
    sess, socks = host
    w, t, out = start_mux()
    send(w, op="add", name="face-0", entry={}, key="K")
    send(w, op="add", name="face-1", entry={}, key="K")
    assert wait(lambda: len(list(socks.glob("*.sock"))) == 2)
    c = socket.socket(socket.AF_UNIX)
    c.connect(str(socks / "unimatrix-face-1.sock"))
    c.sendall(b'{"type":"msg","text":"hi"}\n')
    c.close()
    assert wait(lambda: out.lines)
    assert out.lines == [{"name": "face-1", "frame": {"type": "msg", "text": "hi"}}]
    os.close(w)
    t.join(5)


def test_drop_releases_only_that_name_and_readd_replaces_it(host):
    sess, socks = host
    w, t, out = start_mux()
    for n in ("face-0", "face-1"):
        send(w, op="add", name=n, entry={}, key="K")
    assert wait(lambda: len(list(sess.glob("*.json"))) == 2)
    send(w, op="drop", name="face-0")
    assert wait(lambda: len(list(sess.glob("*.json"))) == 1)
    assert [e["name"] for e in map(lambda p: json.loads(p.read_text()), sess.glob("*.json"))] == ["face-1"]
    send(w, op="add", name="face-1", entry={}, key="K")
    send(w, op="data", name="face-1", line="ignored")
    send(w, op="add", name="face-2", entry={}, key="K")
    assert wait(lambda: len(list(sess.glob("*.json"))) == 2)
    assert sorted(e["name"] for e in map(lambda p: json.loads(p.read_text()), sess.glob("*.json"))) == ["face-1", "face-2"]
    os.close(w)
    t.join(5)


def test_a_refused_name_is_reported_and_the_others_still_register(host, monkeypatch):
    sess, socks = host

    def refuse(name, client="", own_pid=None):
        if name == "face-0":
            sys.exit("name face-0 belongs to a live local session")
        return []

    monkeypatch.setattr(relay, "check_name_free", refuse)
    w, t, out = start_mux()
    send(w, op="add", name="face-0", entry={}, key="K")
    send(w, op="add", name="face-1", entry={}, key="K")
    assert wait(lambda: len(list(sess.glob("*.json"))) == 1 and out.lines)
    assert out.lines == [{"name": "face-0", "error": "name face-0 belongs to a live local session"}]
    os.close(w)
    t.join(5)


def test_a_non_exit_failure_registering_one_name_is_reported_and_the_process_lives(host, monkeypatch):
    sess, socks = host

    def boom(name, client="", own_pid=None):
        if name == "face-0":
            raise OSError("bind failed")
        return []

    monkeypatch.setattr(relay, "check_name_free", boom)
    w, t, out = start_mux()
    send(w, op="add", name="face-0", entry={}, key="K")
    send(w, op="add", name="face-1", entry={}, key="K")
    assert wait(lambda: len(list(sess.glob("*.json"))) == 1 and out.lines)
    assert out.lines == [{"name": "face-0", "error": "OSError: bind failed"}] and t.is_alive()
    os.close(w)
    t.join(5)


def test_mux_entries_are_visible_to_entries_with_their_own_key(host, monkeypatch):
    sess, _ = host
    monkeypatch.setattr(relay, "_liveness", lambda: type("L", (), {
        "entry_live": staticmethod(lambda d: True), "supervisor_live": staticmethod(lambda d: True)}))
    monkeypatch.setattr(relay, "codex_holder_dead", lambda d, h: False)
    pid = os.getpid()
    for n in ("a", "b"):
        path = f"/tmp/x/{n}.sock"
        (sess / f"{pid}.{relay.name_digest(n)}.json").write_text(
            json.dumps({"pid": pid, "name": n, "messagingSocketPath": path}))
        (sess / relay.key_file_name(pid, path)).write_text("k")
    assert sorted(d["name"] for _, d, _ in relay.entries()) == ["a", "b"]
    (sess / relay.key_file_name(pid, "/tmp/x/b.sock")).unlink()
    assert [d["name"] for _, d, _ in relay.entries()] == ["a"]


def test_a_failure_after_bind_releases_the_names_socket_and_entry(host, monkeypatch):
    sess, socks = host
    monkeypatch.setattr(relay.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("ps gone")))
    with pytest.raises(OSError):
        relay.register_name("face-0", {}, "K", mux=True)
    assert list(Path(socks).glob("*.sock")) == [] and list(sess.glob("*.json")) == []
