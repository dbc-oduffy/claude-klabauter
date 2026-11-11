"""Behavior pins for the agy leg on Windows: named-pipe listener, msvcrt spool lock, non-lsof LS discovery,
and the SessionStart peer binding."""
import importlib.util
import json
import os
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
WIN = sys.platform == "win32"
needs_win = pytest.mark.skipif(not WIN, reason="Windows named pipes and msvcrt")


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


leg = load("unimatrix-agy-leg")
launch = load("unimatrix-agy-hook-launch")
sess = load("unimatrix-agy-hook-sessionstart")
CONV = "0cf351a3-6538-4a9b-814c-52b1b75a596c"


@pytest.fixture
def spool(tmp_path, monkeypatch):
    monkeypatch.setenv("AGY_LEG_SPOOL", str(tmp_path / "q.jsonl"))
    monkeypatch.setenv("AGY_LEG_LOG", str(tmp_path / "leg.log"))
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", CONV)
    monkeypatch.setattr(leg, "fcntl", None)  # force the msvcrt path even when imported where fcntl exists
    return tmp_path / "q.jsonl"


def lines(path):
    return [json.loads(x)["text"] for x in path.read_text().splitlines()]


@needs_win
def test_enqueue_and_drain_roundtrip_under_msvcrt_lock(spool, monkeypatch):
    leg.enqueue("a")
    leg.enqueue("b")
    assert lines(spool) == ["a", "b"]
    sent = []
    monkeypatch.setattr(leg, "send", lambda t: sent.append(t) or {"ok": True})
    leg.drain()
    assert sent == ["a", "b"] and spool.read_text() == ""


@needs_win
def test_drain_keeps_the_undelivered_line_and_later_ones(spool, monkeypatch):
    for t in ("a", "b", "c"):
        leg.enqueue(t)
    monkeypatch.setattr(leg, "send", lambda t: {"ok": t == "a"})
    leg.drain()
    assert lines(spool) == ["b", "c"]


@needs_win
def test_enqueue_waits_while_another_holder_has_the_spool_lock(spool):
    done = threading.Event()
    with leg.nt_spool_lock(str(spool)):
        t = threading.Thread(target=lambda: (leg.enqueue("late"), done.set()), daemon=True)
        t.start()
        assert not done.wait(0.4)
    assert done.wait(5)
    assert lines(spool) == ["late"]


@needs_win
def test_spool_lock_times_out_instead_of_hanging(spool, monkeypatch):
    monkeypatch.setattr(leg, "NT_LOCK_TIMEOUT_SECS", 0.2)
    result = {}

    def contend():
        try:
            with leg.nt_spool_lock(str(spool)):
                pass
        except TimeoutError as e:
            result["err"] = e

    with leg.nt_spool_lock(str(spool)):
        t = threading.Thread(target=contend, daemon=True)
        t.start()
        t.join(5)
    assert isinstance(result.get("err"), TimeoutError)


def pipe_roundtrip(endpoint, payload):
    import win32file
    h = win32file.CreateFile(endpoint, win32file.GENERIC_READ | win32file.GENERIC_WRITE, 0, None,
                             win32file.OPEN_EXISTING, 0, None)
    try:
        win32file.WriteFile(h, payload)
        return json.loads(win32file.ReadFile(h, 65536)[1])
    finally:
        win32file.CloseHandle(h)


def wait_for_pipe(endpoint, timeout=5):
    import pywintypes
    import win32pipe
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:  # os.path.exists would connect to the pipe and consume its instance
            win32pipe.WaitNamedPipe(endpoint, 100)
            return
        except pywintypes.error:
            time.sleep(0.05)
    raise AssertionError(f"pipe {endpoint} never appeared")


@needs_win
def test_main_serves_a_named_pipe_and_queues_while_pending(spool, monkeypatch):
    monkeypatch.delenv("ANTIGRAVITY_CSRF_TOKEN", raising=False)
    monkeypatch.setattr(leg, "PARENT_POLL_SECS", 0.05)
    gone = threading.Event()
    monkeypatch.setattr(leg, "parent_gone", gone.is_set)
    endpoint = rf"\\.\pipe\LOCAL\agy-leg-test-{uuid.uuid4().hex}"
    main = threading.Thread(target=leg.main, args=(endpoint,), daemon=True)
    main.start()
    wait_for_pipe(endpoint)
    assert pipe_roundtrip(endpoint, b'{"text": "hello"}\n') == {"ok": True, "queued": True}
    assert pipe_roundtrip(endpoint, b'{"text": "again"}\n') == {"ok": True, "queued": True}
    assert pipe_roundtrip(endpoint, b'not json\n')["ok"] is False
    assert lines(spool) == ["hello", "again"]
    gone.set()
    main.join(5)
    assert not main.is_alive()


@needs_win
def test_main_live_leg_delivers_through_send(spool, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_CSRF_TOKEN", "t")
    monkeypatch.setattr(leg, "PARENT_POLL_SECS", 0.05)
    gone = threading.Event()
    monkeypatch.setattr(leg, "parent_gone", gone.is_set)
    seen = []
    monkeypatch.setattr(leg, "send", lambda t: seen.append(t) or {"ok": True, "status": 200, "body": "x"})
    endpoint = rf"\\.\pipe\LOCAL\agy-leg-test-{uuid.uuid4().hex}"
    main = threading.Thread(target=leg.main, args=(endpoint,), daemon=True)
    main.start()
    wait_for_pipe(endpoint)
    assert pipe_roundtrip(endpoint, b'{"text": "ping"}\n')["ok"] is True
    assert seen == ["ping"]
    gone.set()
    main.join(5)


@needs_win
def test_parent_gone_on_windows_does_not_signal_the_process(monkeypatch):
    monkeypatch.setenv("AGY_LEG_PARENT_PID", str(os.getpid()))
    assert leg.parent_gone() is False  # os.kill(pid, 0) would have terminated this very process
    monkeypatch.setenv("AGY_LEG_PARENT_PID", "4294967")
    assert leg.parent_gone() is True


@needs_win
def test_nt_listener_ports_include_a_loopback_listener_of_this_pid():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        port = srv.getsockname()[1]
        assert port in launch.nt_loopback_listener_ports(os.getpid())
        assert port not in launch.nt_loopback_listener_ports(4294967)
        assert launch.discover_ls_address(os.getpid()).startswith("127.0.0.1:")
    finally:
        srv.close()


def test_discover_ls_address_on_nt_takes_the_lowest_port_without_lsof(monkeypatch):
    monkeypatch.setattr(launch.os, "name", "nt")
    monkeypatch.setattr(launch, "nt_loopback_listener_ports", lambda pid: [50321, 50100] if pid == 42 else [])
    monkeypatch.setattr(launch.subprocess, "run", lambda *a, **k: pytest.fail("spawned a process"))
    assert launch.discover_ls_address(42) == "127.0.0.1:50100"
    assert launch.discover_ls_address(43) is None
    assert launch.discover_ls_address(0) is None


def test_leg_stamps_its_proc_start_into_its_own_record_only(tmp_path, monkeypatch):
    rec = tmp_path / f"{CONV}.json"
    monkeypatch.setenv("AGY_LEG_LOG", str(tmp_path / f"{CONV}.log"))
    leg.stamp_proc_start()  # no record yet: nothing written
    assert not rec.exists()
    rec.write_text(json.dumps({"pid": os.getpid() + 1, "conversation": CONV}))
    leg.stamp_proc_start()  # another process's record is never stamped
    assert "procStart" not in json.loads(rec.read_text())
    rec.write_text(json.dumps({"pid": os.getpid(), "conversation": CONV}))
    leg.stamp_proc_start()
    assert json.loads(rec.read_text())["procStart"] == leg._roster().proc_start(os.getpid())


def test_sessionstart_register_upserts_an_agy_peer_row(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.delenv("ANTIGRAVITY_CSRF_TOKEN", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_LS_ADDRESS", raising=False)

    class FakeProc:
        pid = 31337

    monkeypatch.setattr(launch.subprocess, "Popen", lambda cmd, **kw: FakeProc())
    assert sess.register({"conversationId": CONV, "cwd": str(tmp_path)}, 5) is True
    peers = json.loads((tmp_path / "unimatrix" / "peers.json").read_text())
    peers = peers["peers"] if isinstance(peers, dict) else peers
    assert [(p["vendor"], p["target"]) for p in peers] == [("agy", CONV)]
    assert CONV not in peers[0]["name"] and peers[0]["name"].startswith("agy-")
    rec = json.loads((tmp_path / "unimatrix" / "agy-legs" / f"{CONV}.json").read_text())
    assert rec["cwd"] == str(tmp_path)
    assert sess.register({"conversationId": CONV, "cwd": str(tmp_path)}, 5) is True
    again = json.loads((tmp_path / "unimatrix" / "peers.json").read_text())
    assert len(again["peers"] if isinstance(again, dict) else again) == 1


def test_sessionstart_peer_binding_failure_never_unregisters(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.delenv("ANTIGRAVITY_CSRF_TOKEN", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_LS_ADDRESS", raising=False)

    class FakeProc:
        pid = 31337

    monkeypatch.setattr(launch.subprocess, "Popen", lambda cmd, **kw: FakeProc())
    peers = tmp_path / "unimatrix" / "peers.json"
    peers.parent.mkdir(parents=True)
    peers.write_text("{not json")
    assert sess.register({"conversationId": CONV}, 5) is True
    assert peers.read_text() == "{not json"
