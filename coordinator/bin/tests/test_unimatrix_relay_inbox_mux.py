"""The far relay's `inbox-mux` mode: one process hosts every mirror name of a near box."""
import importlib.util
import io
import json
import os
import shutil
import socket
import sys
import tempfile
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
    def __init__(self) -> None:
        self.lines, self.lock = [], threading.Lock()

    def write(self, b) -> None:
        with self.lock:
            self.lines += [json.loads(x) for x in b.splitlines() if x]

    def flush(self):
        pass


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Measured: the relay's srv.bind() raises OSError "AF_UNIX path too long" (macOS sun_path cap
    # 104) for a socket under pytest's tmp_path (~159 bytes), so serve_mux never registers a name.
    # The socket dir lives in <repo>/scratch (short, doctrine scratch root), removed on teardown.
    scratch = Path(__file__).resolve().parents[3] / "scratch"
    scratch.mkdir(exist_ok=True)
    sock_root = Path(tempfile.mkdtemp(prefix="mx", dir=scratch))
    if len(str(sock_root / "s" / "unimatrix-face-0.sock")) > 100:
        shutil.rmtree(sock_root, ignore_errors=True)
        pytest.skip("repo path too deep for an AF_UNIX socket")
    sess, socks = tmp_path / "sess", sock_root / "s"
    sess.mkdir()
    monkeypatch.setattr(relay, "SESS", str(sess))
    monkeypatch.setattr(relay, "SOCKS", str(socks))
    monkeypatch.setattr(relay, "endpoint_kind", lambda platform=None: "unix")
    monkeypatch.setattr(relay, "client_address", lambda: "10.0.0.9")
    monkeypatch.setattr(relay.subprocess, "run", lambda *a, **k: FakeProc())
    monkeypatch.setattr(relay, "proc_start", lambda pid: None)  # the ps fallback is the injected path

    class Helpers:
        @staticmethod
        def local_peer_key(body, ps, domain) -> str:
            return f"{body}|{ps}|{domain}"

        @staticmethod
        def write_peer_key(path, value) -> None:
            Path(path).write_text(value, encoding="utf-8")

    monkeypatch.setattr(relay, "load_roster", lambda: Helpers)
    monkeypatch.setattr(relay, "check_name_free", lambda name, client="", own_pid=None: [])
    yield sess, socks
    shutil.rmtree(sock_root, ignore_errors=True)


def start_mux():
    r, w = os.pipe()
    out = Out()
    t = threading.Thread(target=relay.serve_mux, args=(r, out), daemon=True)
    t.start()
    return w, t, out


def send(w, **frame) -> None:
    os.write(w, json.dumps(frame).encode() + b"\n")


def wait(pred, secs: int = 5) -> bool:
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_parse_mode_accepts_inbox_mux_and_nothing_wider() -> None:
    assert relay.parse_mode("inbox-mux") == ("inbox-mux", None)
    assert relay.parse_mode("inbox-mux extra") is None
    assert relay.parse_mode("inbox face-0") == ("inbox", "face-0")


def test_one_process_registers_six_names_and_releases_all_on_stdin_eof(host) -> None:
    sess, socks = host
    w, t, out = start_mux()
    for n in NAMES:
        send(w, op="add", name=n, entry={"name": n, "cwd": "/x"}, key="KEYBODY")
    assert wait(lambda: len(list(sess.glob("*.json"))) == 6)
    entries = [json.loads(p.read_text()) for p in sess.glob("*.json")]
    assert {e["pid"] for e in entries} == {os.getpid()}
    assert sorted(e["name"] for e in entries) == sorted(f"{n}.10-0-0-9" for n in NAMES)
    assert len(list(socks.glob("*.sock"))) == 6 and len(list(sess.glob("*.key"))) == 6
    os.close(w)
    t.join(5)
    assert not t.is_alive()
    assert not list(sess.iterdir()) and not list(socks.iterdir())


def test_a_message_to_a_names_socket_is_wrapped_with_that_name(host) -> None:
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


def test_drop_releases_only_that_name_and_readd_replaces_it(host) -> None:
    sess, socks = host
    w, t, out = start_mux()
    for n in ("face-0", "face-1"):
        send(w, op="add", name=n, entry={}, key="K")
    assert wait(lambda: len(list(sess.glob("*.json"))) == 2)
    send(w, op="drop", name="face-0")
    assert wait(lambda: len(list(sess.glob("*.json"))) == 1)
    assert [e["name"] for e in map(lambda p: json.loads(p.read_text()), sess.glob("*.json"))] == ["face-1.10-0-0-9"]
    send(w, op="add", name="face-1", entry={}, key="K")
    send(w, op="data", name="face-1", line="ignored")
    send(w, op="add", name="face-2", entry={}, key="K")
    assert wait(lambda: len(list(sess.glob("*.json"))) == 2)
    assert sorted(e["name"] for e in map(lambda p: json.loads(p.read_text()), sess.glob("*.json"))) == ["face-1.10-0-0-9", "face-2.10-0-0-9"]
    os.close(w)
    t.join(5)


def test_a_refused_name_is_reported_and_the_others_still_register(host, monkeypatch: pytest.MonkeyPatch) -> None:
    sess, socks = host

    def refuse(name, client: str = "", own_pid=None):
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


def test_a_non_exit_failure_registering_one_name_is_reported_and_the_process_lives(host, monkeypatch: pytest.MonkeyPatch) -> None:
    sess, socks = host

    def boom(name, client: str = "", own_pid=None):
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


def test_mux_entries_are_visible_to_entries_with_their_own_key(host, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_a_failure_after_bind_releases_the_names_socket_and_entry(host, monkeypatch: pytest.MonkeyPatch) -> None:
    sess, socks = host
    monkeypatch.setattr(relay, "pid_domain", lambda: (_ for _ in ()).throw(OSError("domain unreadable")))
    with pytest.raises(OSError):
        relay.register_name("face-0", {}, "K", mux=True)
    assert list(Path(socks).glob("*.sock")) == [] and list(sess.glob("*.json")) == []


def test_a_mux_whose_near_client_stops_pinging_releases_every_name_and_socket(host) -> None:
    """A dead TCP path never sends stdin EOF; without the lease the sockets outlive the far session for days
    and a send into one reports success with nobody reading."""
    sess, socks = host
    r, w = os.pipe()
    out = Out()
    t = threading.Thread(target=relay.serve_mux, args=(r, out), kwargs={"lease": 0.4}, daemon=True)
    t.start()
    send(w, op="add", name="face-0", entry={}, key="K")
    os.write(w, relay.PING_LINE)
    assert wait(lambda: len(list(socks.glob("*.sock"))) == 1)
    t.join(5)
    assert not t.is_alive()
    assert not list(socks.glob("*.sock")) and not list(sess.iterdir())
    with pytest.raises(OSError):
        c = socket.socket(socket.AF_UNIX)
        c.connect(str(socks / "unimatrix-face-0.sock"))


def test_a_pinging_near_client_keeps_its_names_past_the_lease(host) -> None:
    sess, socks = host
    r, w = os.pipe()
    t = threading.Thread(target=relay.serve_mux, args=(r, Out()), kwargs={"lease": 0.5}, daemon=True)
    t.start()
    send(w, op="add", name="face-0", entry={}, key="K")
    for _ in range(6):
        os.write(w, relay.PING_LINE)
        time.sleep(0.2)
    assert t.is_alive() and len(list(socks.glob("*.sock"))) == 1
    os.close(w)
    t.join(5)


def test_a_near_client_that_never_pings_is_never_leased_out(host) -> None:
    sess, socks = host
    r, w = os.pipe()
    t = threading.Thread(target=relay.serve_mux, args=(r, Out()), kwargs={"lease": 0.2}, daemon=True)
    t.start()
    send(w, op="add", name="face-0", entry={}, key="K")
    time.sleep(0.6)
    assert t.is_alive() and len(list(socks.glob("*.sock"))) == 1
    os.close(w)
    t.join(5)


def test_a_send_through_a_mirror_socket_writes_an_outbound_delivery_row(host, monkeypatch) -> None:
    """Open row #101: the mirror path recorded nothing, so a lost send left no trace on the sending box."""
    rows = []
    monkeypatch.setattr(relay, "record_delivery", lambda row, now=None: rows.append(row))
    sess, socks = host
    w, t, out = start_mux()
    send(w, op="add", name="face-0", entry={}, key="K")
    assert wait(lambda: len(list(socks.glob("*.sock"))) == 1)
    c = socket.socket(socket.AF_UNIX)
    c.connect(str(socks / "unimatrix-face-0.sock"))
    c.sendall(b'{"type":"msg","text":"secret"}\n')
    c.close()
    assert wait(lambda: rows)
    assert rows[0]["side"] == "mirror-out" and rows[0]["reached"] == "face-0" and rows[0]["ok"] is True
    assert "secret" not in json.dumps(rows)
    os.close(w)
    t.join(5)
