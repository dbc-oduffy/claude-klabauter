"""Delivery proof over the four directed Machine-a/Mini/Machine-b pairs: sender -> bridge -> faked far relay -> sender exit code (tmp_path only, no ssh)."""
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
FAR_NAME = "far-em"
USAGE = b"usage: roster | send <pid> | inbox <name>\n"

PAIRS = [("machine-a", "mini"), ("mini", "machine-a"), ("machine-a", "machine-b"), ("machine-b", "machine-a")]


def _load(filename, name):
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cp(code, out=b"", err=b""):
    return subprocess.CompletedProcess([], code, out, err)


def digest(line):
    return hashlib.sha256(line.rstrip(b"\n")).hexdigest()


class FarRelay:
    """An in-process far relay over its own tmp_path settings home; `ingest` is where a delivered line lands.

    mode: current (acks by digest), drop (replies ok without a digest, writes nothing), lost_reply
    (writes the line, then dies with ssh 255 so only `receipts` can ack it), older (no
    capabilities, no send-session: rejects every verb with usage)."""

    def __init__(self, home, mode="current"):
        self.home, self.mode, self.calls = Path(home), mode, []
        self.home.mkdir(parents=True, exist_ok=True)
        self.ingest = self.home / "ingest.jsonl"
        self.receipts = self.home / "receipts.jsonl"

    def delivered(self):
        return [json.loads(l)["line"] for l in self.ingest.read_text().splitlines()] if self.ingest.exists() else []

    def _write(self, lines):
        with open(self.ingest, "a", encoding="utf-8") as ing, open(self.receipts, "a", encoding="utf-8") as rec:
            for l in lines:
                ing.write(json.dumps({"line": l.decode()}) + "\n")
                rec.write(json.dumps({"session": FAR_SID, "digest": digest(l), "at": 1.0}) + "\n")

    def run(self, command, data=b"", timeout=30):
        return self.handle(command, data, timeout)

    def handle(self, command, data=b"", timeout=30):
        self.calls.append(command.split()[0])
        verb = command.split()[0]
        if self.mode == "older":
            return cp(1, b"", USAGE)
        if verb == "capabilities":
            return cp(0, json.dumps({"ok": True, "capabilities": ["send-session", "receipts", "capabilities"],
                                     "relay_sha": "x"}).encode() + b"\n")
        if verb == "receipts":
            rows = [json.loads(l) for l in self.receipts.read_text().splitlines()] if self.receipts.exists() else []
            return cp(0, json.dumps({"ok": True, "rows": rows}).encode() + b"\n")
        lines = data.splitlines()
        if self.mode == "drop":
            return cp(0, b'{"ok": true}\n' * len(lines))
        self._write(lines)
        if self.mode == "lost_reply":
            return cp(255, b"", b"client_loop: send disconnect")
        return cp(0, b"".join(json.dumps({"ok": True, "digest": digest(l)}).encode() + b"\n" for l in lines))


class Router:
    """The near router: hands the frame to the bridge, then answers the sender as the real one does."""

    ROUTER_ENDPOINT_SEED = "x"

    def __init__(self, bridge):
        self.bridge = bridge

    def endpoint_path(self, seed, d=None):
        return "ctl"

    def request_line(self, ctl, req):
        frame = json.dumps({"from": "uds:" + SENDER, "message": req["text"]}).encode() + b"\n"
        conn = types.SimpleNamespace(chunks=[frame, b""], read=lambda: conn.chunks.pop(0), close=lambda: None)
        self.bridge.relay_send(conn)
        return {"ok": True, "msg_id": "m", "written": True}


@pytest.fixture(params=PAIRS, ids=[f"{a}-to-{b}" for a, b in PAIRS])
def world(request, tmp_path, monkeypatch):
    near, far_box = request.param
    bridge = _load("unimatrix-bridge.py", "unimatrix_bridge_delivery_proof")
    send = _load("unimatrix-send.py", "unimatrix_send_delivery_proof")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    entry = {"pid": os.getpid(), "messagingSocketPath": "/m.sock", "macSocketPath": "/far/4242.sock",
             "name": FAR_NAME, "unimatrixFarName": FAR_NAME, "sessionId": FAR_SID, "unimatrixBox": far_box,
             "unimatrixRemote": f"u@{far_box}.test"}
    (sessions / f"{os.getpid()}.json").write_text(json.dumps(entry))
    monkeypatch.setattr(bridge, "SESSIONS", str(sessions))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / f"{near}-settings"))
    monkeypatch.setattr(bridge._box_mux(), "IS_WIN", False)
    bridge.cfg = types.SimpleNamespace(endpoint="/m.sock", key="k", remote=f"u@{far_box}.test", port=None)
    bridge.log = open(tmp_path / "bridge.log", "a", encoding="utf-8")
    bridge.notices = []
    monkeypatch.setattr(bridge, "notify_sender", lambda line, target, error: bridge.notices.append(error))
    monkeypatch.setattr(send, "_session_entries", lambda: [entry])
    monkeypatch.setattr(send, "own_target", lambda *a, **k: "")
    monkeypatch.setattr(send, "_router", lambda: Router(bridge))

    def run(mode):
        far = FarRelay(tmp_path / f"{far_box}-settings", mode)
        monkeypatch.setattr(bridge, "_ssh_relay", lambda command, data: far.handle(command, data))
        return far

    yield types.SimpleNamespace(bridge=bridge, send=send, run=run, far_box=far_box, tmp=tmp_path)
    bridge.log.close()


def sender(world, capsys, text="hello"):
    rc = world.send.main(["--to", FAR_NAME, text])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_an_acked_far_ingest_gives_sent_and_the_line_is_in_the_far_inbox(world, capsys):
    far = world.run("current")
    rc, out, err = sender(world, capsys)
    assert rc == 0 and out.startswith(f"sent to {FAR_NAME}") and "undelivered" not in err
    assert f"[box {world.far_box}, transport ssh]" in out
    assert len(far.delivered()) == 1 and "hello" in far.delivered()[0]
    assert world.bridge.notices == []


def test_a_missing_receipt_gives_exit_1_and_undelivered_with_the_reason(world, capsys):
    far = world.run("drop")
    rc, out, err = sender(world, capsys)
    assert rc == 1 and "sent to" not in out
    assert f"undelivered: {FAR_NAME}: unconfirmed: no far receipt for this line" in err
    assert f"[box {world.far_box}, transport ssh]" in err
    assert far.delivered() == [] and world.bridge.notices == ["unconfirmed: no far receipt for this line"]


def test_a_lost_reply_is_acked_by_the_far_receipt_and_stays_sent(world, capsys):
    far = world.run("lost_reply")
    rc, out, _ = sender(world, capsys)
    assert rc == 0 and out.startswith("sent to") and len(far.delivered()) == 1
    assert far.calls == ["send-session", "receipts"]


def test_an_older_far_gives_a_named_skew_never_an_attribute_error(world, capsys):
    far = world.run("older")
    rc, out, err = sender(world, capsys)
    assert rc == 1 and "sent to" not in out and "AttributeError" not in err
    assert f"undelivered: {FAR_NAME}: far relay lacks send-session; upgrade it (version skew)" in err
    assert far.calls == ["send-session", "capabilities"] and far.delivered() == []


def test_an_older_far_addresses_without_tcp_connect_names_the_skew_and_still_delivers(world, monkeypatch, capsys):
    """The real _ssh_relay, with only the carrier stubbed: the box-mux dial degrades instead of raising."""
    bridge = world.bridge
    far = FarRelay(world.tmp / f"{world.far_box}-settings")
    mux = bridge._box_mux()
    mux._VERIFIED.clear()
    monkeypatch.setattr(mux, "_far_addresses", lambda: types.SimpleNamespace())
    dialled = []
    monkeypatch.setattr(mux, "channel", lambda box, **kw: dialled.append((box, kw["host"])) or far)
    rc, out, err = sender(world, capsys)
    assert rc == 0 and out.startswith("sent to") and len(far.delivered()) == 1, (rc, out, err)
    assert "lacks tcp_connect" in err and "version skew" in err and "AttributeError" not in err
    assert dialled == [(world.far_box, f"u@{world.far_box}.test")]


def test_the_delivery_row_names_the_far_session_and_the_acked_digest(world, capsys):
    world.run("current")
    sender(world, capsys, "row check")
    (row,) = [json.loads(l) for l in world.bridge.deliveries_path().read_text().splitlines()]
    frame = json.dumps({"from": "uds:" + SENDER, "message": "row check"}).encode()
    assert row["box"] == world.far_box and row["transport"] == "ssh"
    assert row["ok"] is True and row["target"] == f"{FAR_NAME} (pid 4242, session {FAR_SID})"
    assert row["digest"] == digest(frame)


def test_the_dropping_leg_names_the_true_box_and_transport_on_the_error_row(world, capsys):
    """The faked far accepts the write and returns no digest: the recorded row is an error naming the far box and leg."""
    world.run("drop")
    rc, out, err = sender(world, capsys)
    (row,) = [json.loads(l) for l in world.bridge.deliveries_path().read_text().splitlines()]
    assert rc == 1 and "sent to" not in out
    assert row["ok"] is False and row["box"] == world.far_box and row["transport"] == "ssh"
    assert row["error"] == "unconfirmed: no far receipt for this line"
