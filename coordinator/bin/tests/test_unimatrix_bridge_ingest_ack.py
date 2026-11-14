"""The bridge records a relay send ok only when the far receipt digest acks the line; skew is a named outcome (ssh stubbed, tmp_path only)."""
import hashlib
import importlib.util
import json
import os
import subprocess
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
FAR_SID = "5a1f0c2e-1111-4222-8333-444455556666"
SENDER = "/tmp/cc-socks/sender.sock"


class Conn:
    def __init__(self, data):
        self.chunks = [data, b""]

    def read(self):
        return self.chunks.pop(0)

    def close(self):
        pass


def frame(text):
    return json.dumps({"from": "uds:" + SENDER, "message": text}).encode()


def digest(line):
    return hashlib.sha256(line.rstrip(b"\n")).hexdigest()


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("unimatrix_bridge_ingest_ack", BIN / "unimatrix-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "messagingSocketPath": "/m.sock", "macSocketPath": "/far/4242.sock", "name": "far-session",
         "unimatrixFarName": "far-session", "sessionId": FAR_SID}))
    monkeypatch.setattr(mod, "SESSIONS", str(sessions))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setattr(mod._box_mux(), "IS_WIN", False)
    mod.cfg = types.SimpleNamespace(endpoint="/m.sock", key="k", remote="farbox", port=None)
    mod.log = open(tmp_path / "bridge.log", "a", encoding="utf-8")
    mod.notices = []
    monkeypatch.setattr(mod, "notify_sender", lambda line, target, error: mod.notices.append(error))
    yield mod
    mod.log.close()


def far(monkeypatch, responder):
    """Route every ssh call to `responder(command, stdin) -> CompletedProcess`; returns the call log."""
    calls = []

    def run(argv, **k):
        calls.append(argv[-1])
        return responder(argv[-1], k["input"])

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def cp(code, out=b"", err=b""):
    return subprocess.CompletedProcess([], code, out, err)


def ack_replies(data):
    return b"".join(json.dumps({"ok": True, "digest": digest(l)}).encode() + b"\n" for l in data.splitlines())


def rows(bridge):
    return [json.loads(l) for l in bridge.deliveries_path().read_text().splitlines()]


def send(bridge, *texts):
    bridge.relay_send(Conn(b"".join(frame(t) + b"\n" for t in texts)))


def test_a_reply_carrying_the_line_digest_is_ok_with_no_second_round_trip(bridge, monkeypatch):
    calls = far(monkeypatch, lambda cmd, data: cp(0, ack_replies(data)))
    send(bridge, "a", "b")
    assert [r["ok"] for r in rows(bridge)] == [True, True]
    assert len(calls) == 1 and calls[0].startswith("send-session ")
    assert bridge.notices == []


def test_an_ok_reply_without_a_digest_is_unacked(bridge, monkeypatch):
    far(monkeypatch, lambda cmd, data: cp(0, b'{"ok": true}\n'))
    send(bridge, "a")
    (row,) = rows(bridge)
    assert row["ok"] is False and row["error"] == "unconfirmed: no far receipt for this line"
    assert bridge.notices == ["unconfirmed: no far receipt for this line"]


def test_an_ok_reply_with_another_lines_digest_is_unacked(bridge, monkeypatch):
    far(monkeypatch, lambda cmd, data: cp(0, json.dumps({"ok": True, "digest": digest(b"other")}).encode() + b"\n"))
    send(bridge, "a")
    assert rows(bridge)[0]["error"] == "unconfirmed: no far receipt for this line"


def test_a_refused_line_keeps_its_relay_error_and_the_acked_line_stays_ok(bridge, monkeypatch):
    def respond(cmd, data):
        first, second = data.splitlines()
        return cp(0, json.dumps({"ok": True, "digest": digest(first)}).encode() + b"\n"
                  + b'{"ok": false, "error": "ConnectionRefusedError: gone"}\n')

    far(monkeypatch, respond)
    send(bridge, "a", "b")
    got = rows(bridge)
    assert [r["ok"] for r in got] == [True, False] and got[1]["error"] == "relay: ConnectionRefusedError: gone"


def test_a_lost_reply_after_write_is_acked_by_one_receipts_read(bridge, monkeypatch):
    seen = {}

    def respond(cmd, data):
        if cmd.startswith("send-session"):
            seen["data"] = data
            return cp(255, b"", b"client_loop: send disconnect")
        return cp(0, json.dumps({"ok": True, "rows": [{"session": FAR_SID, "digest": digest(l), "at": 1.0}
                                                      for l in seen["data"].splitlines()]}).encode() + b"\n")

    calls = far(monkeypatch, respond)
    send(bridge, "a", "b")
    assert [r["ok"] for r in rows(bridge)] == [True, True]
    receipts = [c for c in calls if c.startswith("receipts ")]
    assert len(receipts) == 1 and receipts[0].split()[1] == FAR_SID
    assert float(receipts[0].split()[2]) <= bridge.time.time() - 59
    assert "capabilities" not in calls


def test_a_lost_reply_with_no_matching_receipt_stays_a_failure_with_the_ssh_error(bridge, monkeypatch):
    def respond(cmd, data):
        if cmd.startswith("send-session"):
            return cp(255, b"", b"client_loop: send disconnect")
        return cp(0, b'{"ok": true, "rows": []}\n')

    far(monkeypatch, respond)
    send(bridge, "a")
    (row,) = rows(bridge)
    assert row["ok"] is False and row["error"].startswith("ssh exit 255") and "send disconnect" in row["error"]


def test_a_failed_receipts_read_leaves_the_ssh_failure(bridge, monkeypatch):
    far(monkeypatch, lambda cmd, data: cp(255, b"", b"Too many authentication failures"))
    send(bridge, "a")
    (row,) = rows(bridge)
    assert row["ok"] is False and "Too many authentication failures" in row["error"]


def test_a_reply_missing_a_line_is_reconciled_per_line(bridge, monkeypatch):
    seen = {}

    def respond(cmd, data):
        if cmd.startswith("send-session"):
            seen["lines"] = data.splitlines()
            return cp(255, json.dumps({"ok": True, "digest": digest(seen["lines"][0])}).encode() + b"\n", b"cut")
        return cp(0, json.dumps({"ok": True, "rows": [{"digest": digest(seen["lines"][1])}]}).encode() + b"\n")

    far(monkeypatch, respond)
    send(bridge, "a", "b")
    assert [r["ok"] for r in rows(bridge)] == [True, True]


@pytest.mark.parametrize("capabilities_reply", [
    cp(1, b"", b"usage: roster | send <pid>\n"),
    cp(0, b'{"ok": true, "capabilities": ["receipts", "capabilities"], "relay_sha": "x"}\n'),
])
def test_a_far_relay_lacking_send_session_records_the_named_skew(bridge, monkeypatch, capabilities_reply):
    def respond(cmd, data):
        return cp(1, b"", b"usage: roster | send <pid>\n") if cmd.startswith("send-session") else capabilities_reply

    calls = far(monkeypatch, respond)
    send(bridge, "a", "b")
    errors = {r["error"] for r in rows(bridge)}
    assert errors == {"far relay lacks send-session; upgrade it (version skew)"}
    assert [c for c in calls if c == "capabilities"] == ["capabilities"]


def test_a_relay_that_serves_send_session_but_failed_is_not_called_skew(bridge, monkeypatch):
    def respond(cmd, data):
        if cmd.startswith("send-session"):
            return cp(1, b"", b"no live session with pid 4242\n")
        return cp(0, b'{"ok": true, "capabilities": ["send-session", "receipts", "capabilities"]}\n')

    far(monkeypatch, respond)
    send(bridge, "a")
    (row,) = rows(bridge)
    assert row["ok"] is False and "skew" not in row["error"]


def test_capabilities_is_not_asked_on_a_successful_send_or_a_named_refusal(bridge, monkeypatch):
    calls = far(monkeypatch, lambda cmd, data: cp(0, ack_replies(data)))
    send(bridge, "a")
    calls2 = far(monkeypatch, lambda cmd, data: cp(1, b"", b"target mismatch: pid 4242 holds other\n"))
    send(bridge, "a")
    assert calls == [calls[0]] and calls2 == [calls2[0]]


def test_every_recorded_row_names_the_mirror_box_and_the_ssh_leg(bridge, monkeypatch):
    sess = Path(bridge.SESSIONS) / f"{os.getpid()}.json"
    d = json.loads(sess.read_text())
    sess.write_text(json.dumps({**d, "unimatrixBox": "macmini"}))
    far(monkeypatch, lambda cmd, data: cp(0, ack_replies(data)))
    send(bridge, "a")
    (row,) = rows(bridge)
    assert row["box"] == "macmini" and row["transport"] == "ssh"


def test_the_tunnel_leg_is_labelled_tunnel(bridge, monkeypatch):
    bridge.cfg.tunnel_remote = "tun"
    monkeypatch.setattr(bridge, "pick_remote", lambda lan, tunnel: tunnel)
    far(monkeypatch, lambda cmd, data: cp(0, ack_replies(data)))
    send(bridge, "a")
    assert rows(bridge)[0]["transport"] == "tunnel"


def test_a_refusal_before_any_ssh_records_transport_none_and_box_unknown(bridge, monkeypatch):
    sess = Path(bridge.SESSIONS) / f"{os.getpid()}.json"
    d = json.loads(sess.read_text())
    d.pop("unimatrixFarName")
    sess.write_text(json.dumps(d))
    calls = far(monkeypatch, lambda cmd, data: cp(0, ack_replies(data)))
    send(bridge, "a")
    (row,) = rows(bridge)
    assert calls == [] and row["transport"] == "none" and row["box"] == "unknown"
