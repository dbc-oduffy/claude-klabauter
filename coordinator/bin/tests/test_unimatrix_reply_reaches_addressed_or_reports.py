"""A far reply that lands on a session other than the addressed one is refused and reported; a landed one acks the receiver."""
import hashlib
import importlib.util
import json
import os
import subprocess
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
SENT = b'{"from": "uds:/s.sock", "message": "hi"}'
DIGEST = hashlib.sha256(SENT).hexdigest()


class Conn:
    def __init__(self) -> None:
        self.chunks = [SENT + b"\n", b""]

    def read(self):
        return self.chunks.pop(0)

    def write(self, data) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location("unimatrix_bridge_reply_reaches", BIN / "unimatrix-bridge.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "name": "coordinator-content-repo-9f", "messagingSocketPath": "/m.sock", "macSocketPath": "/far/9.sock",
         "unimatrixRemote": "lan", "unimatrixFarName": "coordinator-content-repo-9f", "sessionId": "sid-far", "box": "Machine-b"}))
    monkeypatch.setattr(bridge, "SESSIONS", str(sessions))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    bridge.cfg = types.SimpleNamespace(endpoint="/m.sock", key="k", remote="lan", tunnel_remote=None, port=None)
    bridge.log = open(tmp_path / "b.log", "a", encoding="utf-8")
    bridge.sent = []
    monkeypatch.setattr(bridge, "write_local", lambda endpoint, frame: bridge.sent.append(json.loads(frame)["message"]))
    yield bridge
    bridge.log.close()


def drive(bridge, monkeypatch, reply):
    stdout = json.dumps({"ok": True, "digest": DIGEST, **reply}).encode() + b"\n"
    monkeypatch.setattr(bridge, "_ssh_relay", lambda command, data: subprocess.CompletedProcess([], 0, stdout, b""))
    bridge.serve_one(Conn())
    return [json.loads(l) for l in bridge.deliveries_path().read_text().splitlines()]


def test_a_reply_that_reached_a_differently_named_session_is_refused_and_reported(env, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = drive(env, monkeypatch, {"reached": "example-retrieval-repo-ue-addon-38", "reached_box": "Machine-b"})
    assert [r["ok"] for r in rows] == [False]
    assert "example-retrieval-repo-ue-addon-38" in rows[0]["error"] and "Machine-b" in rows[0]["error"] and "coordinator-content-repo-9f" in rows[0]["error"]
    assert rows[0]["error"].startswith(env.TARGET_MISMATCH)
    assert len(env.sent) == 1 and "Misdelivered" in env.sent[0] and "example-retrieval-repo-ue-addon-38" in env.sent[0]
    assert "It was not delivered" not in env.sent[0]
    assert (Path(env.SESSIONS) / f"{os.getpid()}.misroute").exists()


@pytest.mark.parametrize("reached", ["coordinator-content-repo-9f", "coordinator-content-repo-9f.lan"])
def test_a_reply_that_reached_the_addressed_session_acks_the_receiver(env, monkeypatch: pytest.MonkeyPatch, reached) -> None:
    rows = drive(env, monkeypatch, {"reached": reached, "reached_box": "Machine-b"})
    assert [r["ok"] for r in rows] == [True] and rows[0]["reached"] == reached
    assert env.sent == [f"Delivered to {reached} on Machine-b."]
    assert not (Path(env.SESSIONS) / f"{os.getpid()}.misroute").exists()


def test_a_far_relay_that_does_not_name_the_receiver_acks_it_unconfirmed(env, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = drive(env, monkeypatch, {})
    assert [r["ok"] for r in rows] == [True] and "reached" not in rows[0]
    assert len(env.sent) == 1 and "receiver unconfirmed" in env.sent[0]
    assert not (Path(env.SESSIONS) / f"{os.getpid()}.misroute").exists()


def test_a_failed_line_gets_the_not_delivered_notice_not_an_ack(env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(env, "_ssh_relay", lambda command, data: subprocess.CompletedProcess([], 255, b"", b"down"))
    env.serve_one(Conn())
    assert len(env.sent) == 1 and "It was not delivered" in env.sent[0]
