"""Relay send failure: sender inbox notice and the delivery status record (ssh stubbed, tmp_path only)."""
import importlib.util
import json
import subprocess
import types
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[1] / "unimatrix-bridge.py"
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
    (sessions / "1.json").write_text(json.dumps(
        {"messagingSocketPath": MIRROR, "macSocketPath": "/far/4242.sock", "name": "far-session"}))
    monkeypatch.setattr(mod, "SESSIONS", str(sessions))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    mod.cfg = types.SimpleNamespace(endpoint=MIRROR, key="k", remote="farbox", port=None)
    mod.log = open(tmp_path / "bridge.log", "a", encoding="utf-8")
    mod.written = []
    monkeypatch.setattr(mod, "write_local", lambda ep, data: mod.written.append((ep, data)))
    return mod


def stub_ssh(monkeypatch, code, stderr=b""):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, code, b"", stderr))


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


def test_ssh_timeout_is_a_failed_delivery_with_a_notice(bridge, monkeypatch):
    def hang(*a, **k):
        raise subprocess.TimeoutExpired("ssh", 30)

    monkeypatch.setattr(subprocess, "run", hang)
    send(bridge)
    assert len(bridge.written) == 1 and "TimeoutExpired" in json.loads(bridge.written[0][1])["message"]
    assert bridge.delivery_status()[0].startswith("FAILED")
