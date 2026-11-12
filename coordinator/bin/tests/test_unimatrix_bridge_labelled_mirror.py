"""relay_send accepts exactly the mirror names the roster writes; any other name is a misroute and notifies the sender."""
import hashlib
import importlib.util
import json
import os
import subprocess
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
MIRROR = "/tmp/cc-socks/mirror.sock"
SENDER = "/tmp/cc-socks/sender.sock"
FAR_SID = "5a1f0c2e-1111-4222-8333-444455556666"
UUID = "0a1b2c3d-1111-2222-3333-444455556666"


class FakeConn:
    def __init__(self, data):
        self.chunks = [data, b""]

    def read(self):
        return self.chunks.pop(0)

    def close(self):
        pass


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, BIN / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    mod = load("unimatrix_bridge_labelled", "unimatrix-bridge.py")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(mod, "SESSIONS", str(sessions))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setattr(mod._box_mux(), "IS_WIN", False)
    mod.cfg = types.SimpleNamespace(endpoint=MIRROR, key="k", remote="me@farbox", port=None, tunnel_remote=None)
    mod.log = open(tmp_path / "bridge.log", "a", encoding="utf-8")
    mod.written = []
    monkeypatch.setattr(mod, "write_local", lambda ep, data: mod.written.append((ep, data)))
    monkeypatch.setattr(mod, "pick_remote", lambda lan, tun: lan)
    mod.sessions = sessions
    yield mod
    mod.log.close()


def entry(bridge, name, far_name, **extra):
    (bridge.sessions / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "messagingSocketPath": MIRROR, "macSocketPath": "/far/4242.sock", "name": name,
         "unimatrixFarName": far_name, "sessionId": FAR_SID, **extra}))


def send(bridge, monkeypatch, ok=True):
    calls = []

    def run(argv, **k):
        calls.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, b'{"ok": true}\n', b"")

    monkeypatch.setattr(subprocess, "run", run)
    bridge.relay_send(FakeConn(json.dumps({"from": "uds:" + SENDER, "message": "hi"}).encode() + b"\n"))
    return calls


def misroute_marker(bridge):
    return bridge.sessions / f"{os.getpid()}.misroute"


def test_a_mirror_named_labelled_for_this_box_is_delivered_without_a_marker(bridge, monkeypatch):
    entry(bridge, "group-em-3e.farbox", "group-em-3e")
    calls = send(bridge, monkeypatch)
    assert len(calls) == 1 and calls[0].startswith("send-session 4242 ")
    rows = [json.loads(l) for l in bridge.deliveries_path().read_text().splitlines()]
    assert [r["ok"] for r in rows] == [True]
    assert not misroute_marker(bridge).exists()
    assert bridge.written == []


@pytest.mark.parametrize("name, far_name", [
    ("far-session", "far-session"),
    ("far-session.farbox", "far-session"),
    ("a-b.farbox", "a.b"),
    ("a-b", "a.b"),
    ("a.farbox", "a.farbox"),
])
def test_every_name_the_roster_writes_for_the_far_name_is_accepted(bridge, monkeypatch, name, far_name):
    entry(bridge, name, far_name)
    assert len(send(bridge, monkeypatch)) == 1
    assert not misroute_marker(bridge).exists()


def test_legacy_vendor_alias_is_accepted(bridge, monkeypatch):
    short = hashlib.sha256(UUID.encode()).hexdigest()[:10]
    entry(bridge, f"codex-myrepo-{short}-on-farbox", f"codex-{UUID}")
    assert len(send(bridge, monkeypatch)) == 1
    assert not misroute_marker(bridge).exists()


@pytest.mark.parametrize("name, far_name", [
    ("far-session.otherbox", "far-session"),
    ("other-session", "far-session"),
    ("other-session.farbox", "far-session"),
    ("far-session.farbox.farbox", "far-session"),
    ("codex-myrepo-0000000000-on-farbox", f"codex-{UUID}"),
    ("codex-myrepo-onbox", f"codex-{UUID}"),
])
def test_any_other_name_still_refuses_marks_misroute_and_notifies_the_sender(bridge, monkeypatch, name, far_name):
    entry(bridge, name, far_name)
    assert send(bridge, monkeypatch) == []
    assert misroute_marker(bridge).exists()
    (endpoint, frame), = bridge.written
    notice = json.loads(frame)
    assert endpoint == SENDER and notice["from"] == "uds:" + MIRROR
    assert "re-mirror" in notice["message"] and "did not reach the far box" in notice["message"]
    (row,) = [json.loads(l) for l in bridge.deliveries_path().read_text().splitlines()]
    assert row["ok"] is False and "re-mirror" in row["error"]


@pytest.mark.parametrize("extra, why", [({"unimatrixFarName": ""}, "far name"), ({"sessionId": ""}, "session id")])
def test_the_lacks_refusals_also_notify_the_sender(bridge, monkeypatch, extra, why):
    entry(bridge, "far-session.farbox", "far-session")
    path = bridge.sessions / f"{os.getpid()}.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), **extra}))
    assert send(bridge, monkeypatch) == []
    (endpoint, frame), = bridge.written
    assert endpoint == SENDER and why in json.loads(frame)["message"]


def test_every_delivery_row_carries_the_digest_of_its_line(bridge, monkeypatch):
    entry(bridge, "far-session.farbox", "far-session")
    line = json.dumps({"from": "uds:" + SENDER, "message": "hi"}).encode()
    send(bridge, monkeypatch)
    (ok_row,) = [json.loads(l) for l in bridge.deliveries_path().read_text().splitlines()]
    assert ok_row["digest"] == hashlib.sha256(line).hexdigest()
    entry(bridge, "other.farbox", "far-session")
    send(bridge, monkeypatch)
    failed = [json.loads(l) for l in bridge.deliveries_path().read_text().splitlines()][-1]
    assert failed["ok"] is False and failed["digest"] == ok_row["digest"]
