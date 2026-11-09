"""Relay send failure: sender inbox notice and the delivery status record (ssh stubbed, tmp_path only)."""
import importlib.util
import json
import os
import subprocess
import types
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[1] / "unimatrix-bridge.py"
RELAY = BRIDGE.with_name("unimatrix-relay.py")
MIRROR = "/tmp/cc-socks/mirror.sock"
SENDER = "/tmp/cc-socks/sender.sock"


class FakeConn:
    def __init__(self, data):
        self.chunks = [data, b""]

    def read(self):
        return self.chunks.pop(0)

    def close(self):
        pass


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("unimatrix_bridge_delivery", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "messagingSocketPath": MIRROR, "macSocketPath": "/far/4242.sock", "name": "far-session"}))
    monkeypatch.setattr(mod, "SESSIONS", str(sessions))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    mod.cfg = types.SimpleNamespace(endpoint=MIRROR, key="k", remote="farbox", port=None)
    mod.log = open(tmp_path / "bridge.log", "a", encoding="utf-8")
    mod.written = []
    monkeypatch.setattr(mod, "write_local", lambda ep, data: mod.written.append((ep, data)))
    return mod


def test_supervisor_argument_is_parsed_and_optional(bridge):
    assert bridge.parse_args(["ep", "relay", "b.log", "--remote", "r", "--key", "k", "--supervisor", "77"]).supervisor == 77
    assert bridge.parse_args(["ep", "relay", "b.log", "--remote", "r", "--key", "k"]).supervisor is None


def test_bridge_exits_and_drops_its_files_when_the_supervisor_is_gone(bridge, tmp_path, monkeypatch):
    sessions = Path(bridge.SESSIONS)
    me = os.getpid()
    (sessions / f"{me}.json").write_text(json.dumps({"pid": me, "unimatrixRosterProcStart": "boot-1"}))
    (sessions / f"{me}.abc.key").write_text("k")
    (sessions / "99999999.json").write_text("{}")
    monkeypatch.setattr(bridge, "IS_WIN", True)
    bridge.cfg = types.SimpleNamespace(endpoint=MIRROR, supervisor=4321)
    lv = bridge._roster()._liveness()
    monkeypatch.setattr(lv, "pid_alive", lambda p: True)
    monkeypatch.setattr(lv, "proc_start", lambda p, platform=None: "boot-1")
    assert bridge.supervisor_gone() is False
    monkeypatch.setattr(lv, "proc_start", lambda p, platform=None: "recycled")
    assert bridge.supervisor_gone() is True
    monkeypatch.setattr(lv, "pid_alive", lambda p: False)
    assert bridge.supervisor_gone() is True
    bridge.drop_own_files()
    assert sorted(p.name for p in sessions.iterdir()) == ["99999999.json"]


def test_watcher_exits_the_process_once_the_supervisor_is_gone(bridge, monkeypatch):
    exits = []

    class Stop(Exception):
        pass

    def fake_exit(code):
        exits.append(code)
        raise Stop

    bridge.cfg = types.SimpleNamespace(endpoint=MIRROR, supervisor=4321)
    monkeypatch.setattr(bridge, "IS_WIN", True)
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)
    monkeypatch.setattr(bridge, "supervisor_gone", lambda: True)
    monkeypatch.setattr(bridge.os, "_exit", fake_exit)
    with pytest.raises(Stop):
        bridge.watch_supervisor()
    assert exits == [0]


def stub_ssh(monkeypatch, code, stderr=b""):
    out = b'{"ok": true}\n' if code == 0 else b""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, code, out, stderr))


def send(bridge):
    line = json.dumps({"from": "uds:" + SENDER, "message": "hi"}).encode()
    bridge.relay_send(FakeConn(line + b"\n"))


def test_failed_relay_notifies_the_sender_and_status_reports_failed(bridge, monkeypatch):
    stub_ssh(monkeypatch, 255, b"Too many authentication failures")
    send(bridge)
    (endpoint, frame), = bridge.written
    notice = json.loads(frame)
    assert endpoint == SENDER
    assert notice["from"] == "uds:" + MIRROR
    assert "far-session" in notice["message"] and "255" in notice["message"]
    assert "Too many authentication failures" in notice["message"]
    status = bridge.delivery_status()
    assert len(status) == 1 and status[0].startswith("FAILED") and "255" in status[0]


def test_successful_relay_sends_no_notice_and_status_reports_delivered(bridge, monkeypatch):
    stub_ssh(monkeypatch, 0)
    send(bridge)
    assert bridge.written == []
    assert [s.split()[0] for s in bridge.delivery_status()] == ["delivered"]


def test_unknown_sender_logs_instead_of_raising(bridge, monkeypatch):
    stub_ssh(monkeypatch, 255, b"boom")
    bridge.relay_send(FakeConn(b'{"message": "hi"}\n'))
    assert bridge.written == []
    assert bridge.delivery_status()[0].startswith("FAILED")


def test_notice_write_failure_does_not_break_the_relay(bridge, monkeypatch):
    stub_ssh(monkeypatch, 255, b"x")

    def boom(ep, data):
        raise OSError("inbox refused")

    monkeypatch.setattr(bridge, "write_local", boom)
    send(bridge)
    assert bridge.delivery_status()[0].startswith("FAILED")


def test_status_is_bounded_and_empty_record_is_stated(bridge, monkeypatch):
    assert bridge.delivery_status() == ["no cross-box sends recorded"]
    stub_ssh(monkeypatch, 0)
    for _ in range(bridge.DELIVERY_KEEP + 5):
        send(bridge)
    assert len(bridge.deliveries_path().read_text().splitlines()) == bridge.DELIVERY_KEEP


def test_status_command_runs_at_default_arguments(bridge, monkeypatch, capsys):
    stub_ssh(monkeypatch, 255, b"denied")
    send(bridge)
    bridge.main(["status"])
    assert capsys.readouterr().out.startswith("FAILED")


def send_many(bridge, n):
    lines = b"".join(json.dumps({"from": "uds:" + SENDER, "message": f"m{i}"}).encode() + b"\n" for i in range(n))
    bridge.relay_send(FakeConn(lines))


def record_ssh(monkeypatch, responder):
    calls = []

    def run(argv, **k):
        calls.append((argv[-1], k["input"]))
        return responder(argv[-1], k["input"])

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_n_lines_go_in_one_ssh_and_each_gets_a_record(bridge, monkeypatch):
    replies = b'{"ok": true}\n{"ok": false, "error": "ConnectionRefusedError: gone"}\n{"ok": true}\n'
    calls = record_ssh(monkeypatch, lambda cmd, data: subprocess.CompletedProcess([], 0, replies, b""))
    send_many(bridge, 3)
    assert [c[0] for c in calls] == ["send-batch 4242"]
    assert calls[0][1].count(b"\n") == 3
    assert [s.split()[0] for s in bridge.delivery_status()] == ["delivered", "FAILED", "delivered"]
    (endpoint, frame), = bridge.written
    assert endpoint == SENDER and "gone" in json.loads(frame)["message"]


def test_old_relay_usage_error_falls_back_to_per_line_send(bridge, monkeypatch):
    def responder(cmd, data):
        if cmd.startswith("send-batch"):
            return subprocess.CompletedProcess([], 1, b"", b"usage: roster | send <pid> | inbox <name>\n")
        return subprocess.CompletedProcess([], 0, b"", b"")

    calls = record_ssh(monkeypatch, responder)
    send_many(bridge, 2)
    assert [c[0] for c in calls] == ["send-batch 4242", "send 4242", "send 4242"]
    assert [s.split()[0] for s in bridge.delivery_status()] == ["delivered", "delivered"]


def test_a_relay_refusal_is_recorded_failed_and_not_retried_per_line(bridge, monkeypatch):
    calls = record_ssh(monkeypatch, lambda cmd, data: subprocess.CompletedProcess(
        [], 1, b"", b"no live session with pid 4242\n"))
    send_many(bridge, 2)
    assert [c[0] for c in calls] == ["send-batch 4242"]
    status = bridge.delivery_status()
    assert [s.split()[0] for s in status] == ["FAILED", "FAILED"] and "no live session" in status[0]
    assert len(bridge.written) == 2


def test_relay_mode_parses_send_batch_and_rejects_non_digit_pid():
    spec = importlib.util.spec_from_file_location("unimatrix_relay_batch", RELAY)
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)
    assert relay.parse_mode("send-batch 77") == ("send-batch", 77)
    assert relay.parse_mode("send 77") == ("send", 77)
    assert relay.parse_mode("send-batch x") is None
    assert relay.parse_mode("send-batch") is None


def test_relay_send_batch_replies_per_line_and_continues_past_a_fault(monkeypatch, capsys):
    import io

    spec = importlib.util.spec_from_file_location("unimatrix_relay_batch2", RELAY)
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)
    seen = []

    def write(path, line):
        if b"bad" in line:
            raise ConnectionRefusedError("gone")
        seen.append(line)

    monkeypatch.setattr(relay, "_session_path", lambda pid: "/p")
    monkeypatch.setattr(relay, "_write_line", write)
    monkeypatch.setattr(relay.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"a\nbad\n\nc\n")))
    relay.send_batch(1)
    out = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert [o["ok"] for o in out] == [True, False, False, True] and "ConnectionRefusedError" in out[1]["error"]
    assert "empty message line" in out[2]["error"]
    assert seen == [b"a\n", b"c\n"]


def test_ssh_timeout_is_a_failed_delivery_with_a_notice(bridge, monkeypatch):
    def hang(*a, **k):
        raise subprocess.TimeoutExpired("ssh", 30)

    monkeypatch.setattr(subprocess, "run", hang)
    send(bridge)
    assert len(bridge.written) == 1 and "TimeoutExpired" in json.loads(bridge.written[0][1])["message"]
    assert bridge.delivery_status()[0].startswith("FAILED")
