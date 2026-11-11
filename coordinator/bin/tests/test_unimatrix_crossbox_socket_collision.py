"""Socket names are local to a box: identical far paths must never cross-route."""
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

BIN = Path(__file__).resolve().parents[1]
FAR = "/tmp/cc-socks/cc-msg-086bf13a2ac11a310327603bcc2288c5.sock"


def load(name):
    spec = importlib.util.spec_from_file_location("socket_collision_" + name, BIN / f"unimatrix-{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_two_boxes_same_far_socket_receive_distinct_local_endpoints(tmp_path, monkeypatch):
    roster = load("roster")
    processes = iter([31337, 31338])
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(tmp_path))
    monkeypatch.setattr(roster, "spawn", lambda cmd: SimpleNamespace(pid=next(processes)))
    monkeypatch.setattr(roster, "proc_start", lambda pid: "local-start")
    monkeypatch.setattr(roster.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(roster, "_is_win", lambda platform=None: True)
    for remote, far_pid in [("user@192.168.0.175", 80954), ("user@192.168.0.116", 62725)]:
        cfg = SimpleNamespace(remote=remote, key="key", tunnel_remote=None, log=str(tmp_path / "log"))
        row = {"entry": {"pid": far_pid, "name": "codex-claude-klabauter", "messagingSocketPath": FAR}, "key": "a" * 32}
        roster.mirror(row, cfg, lambda *a: None)
    one, two = [json.loads((tmp_path / f"{pid}.json").read_text()) for pid in [31337, 31338]]
    assert one["messagingSocketPath"] != two["messagingSocketPath"]
    assert one["macSocketPath"] == two["macSocketPath"] == FAR
    assert one["unimatrixRemote"] != two["unimatrixRemote"]
    assert (one["farPid"], two["farPid"]) == (80954, 62725)


def test_bridge_uses_own_registry_pid_even_with_same_endpoint_decoy(tmp_path, monkeypatch):
    bridge = load("bridge")
    endpoint = "local-endpoint"
    (tmp_path / "1.json").write_text(json.dumps({"pid": 1, "messagingSocketPath": endpoint,
        "macSocketPath": FAR, "farPid": 80954, "unimatrixRemote": "user@192.168.0.175"}))
    (tmp_path / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid(), "messagingSocketPath": endpoint,
        "macSocketPath": FAR, "farPid": 62725, "unimatrixRemote": "user@192.168.0.116",
        "unimatrixFarName": "far", "sessionId": "sid-62725"}))
    monkeypatch.setattr(bridge, "SESSIONS", str(tmp_path))
    bridge.cfg = SimpleNamespace(endpoint=endpoint, key="key", remote="user@192.168.0.116", tunnel_remote=None)
    bridge.log = io.StringIO()
    sent = []
    monkeypatch.setattr(bridge, "pick_remote", lambda remote, tunnel: remote)
    monkeypatch.setattr(bridge, "record_delivery", lambda row: None)
    monkeypatch.setattr(bridge.subprocess, "run", lambda argv, **kw: sent.append(argv) or subprocess.CompletedProcess(argv, 0, b"", b""))
    connection = SimpleNamespace(read=iter([b'{"type":"user"}\n', b""]).__next__, close=lambda: None)
    bridge.relay_send(connection)
    assert sent[0][-2] == "user@192.168.0.116"
    assert re.fullmatch(r"send-session 62725 [0-9a-f]{16} [0-9a-f]{16} sid-62725", sent[0][-1])


def mirror(remote, endpoint, port):
    return {"unimatrixRemote": "user@" + remote, "macSocketPath": FAR,
            "messagingSocketPath": endpoint, "injectPort": port, "name": "same-name"}


def test_inbox_reply_localization_and_return_port_are_scoped_to_box(monkeypatch):
    inbox = load("inbox")
    mirrors = [mirror("192.168.0.175", "mac-mini-pipe", 101), mirror("192.168.0.116", "macbook-pipe", 202)]
    monkeypatch.setattr(inbox, "_mirrors", lambda: iter(mirrors))
    frame = {"from": "uds:" + FAR, "message": {"content": "message"}}
    line = json.dumps(frame).encode()
    assert json.loads(inbox.localize(dict(frame), line, "192.168.0.116"))["from"] == "uds:macbook-pipe"
    assert inbox.inject_port(frame, "192.168.0.175") == 101
    assert inbox.inject_port(frame, "192.168.0.116") == 202
    assert inbox.localize(dict(frame), line) == line
    assert inbox.inject_port(frame) == inbox.DEFAULT_PORT


def test_relay_reply_lookup_uses_ssh_client_identity(monkeypatch):
    relay = load("relay")
    entries = [(1, {"unimatrixMirror": True, "unimatrixRemoteSocket": FAR,
                    "unimatrixClient": "192.168.0.175", "messagingSocketPath": "mini"}, "key"),
               (2, {"unimatrixMirror": True, "unimatrixRemoteSocket": FAR,
                    "unimatrixClient": "192.168.0.116", "messagingSocketPath": "macbook"}, "key")]
    monkeypatch.setattr(relay, "entries", lambda: iter(entries))
    monkeypatch.setattr(relay, "client_address", lambda: "192.168.0.116")
    line = json.dumps({"from": "uds:" + FAR}).encode()
    assert json.loads(relay.local_reply_address(line))["from"] == "uds:macbook"
    monkeypatch.setattr(relay, "client_address", lambda: "")
    assert relay.local_reply_address(line) == line
