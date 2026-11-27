"""A send to a non-Claude peer is delivered or refused with a named reason; a row that cannot receive is marked or withdrawn.

Falsifier for the outbound reply path: `relay_send` (unimatrix-bridge.py) and the roster's `listed_row` /
`session_identity` (unimatrix-roster.py) are driven at default arguments, over a uds and a pipe endpoint.
"""
import importlib.util
import json
import os
import subprocess
import time
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
ENDPOINTS = {
    "uds": ("/tmp/cc-socks/cc-msg-near.sock", "/far/cc-socks/9.sock"),
    "pipe": (r"\\.\pipe\LOCAL\cc-msg-near", r"\\.\pipe\LOCAL\cc-msg-far"),
}


def load(rel, modname):
    spec = importlib.util.spec_from_file_location(modname, BIN / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REASON = load("_unimatrix/delivery_reason.py", "unimatrix_delivery_reason")


def test_refuse_names_a_known_reason_and_rejects_an_unnamed_one() -> None:
    assert REASON.refuse("session_gone", "x") == {"ok": False, "reason": "session_gone", "error": "x"}
    for needed in ("no_leg_listening", "transport_face_not_addressable", "session_gone", "socket_timeout", "far_unreachable"):
        assert needed in REASON.REASONS
    with pytest.raises(ValueError):
        REASON.refuse("bare failure", "x")


class Conn:
    def __init__(self) -> None:
        self.chunks = [b'{"from": "uds:/s.sock", "message": "hi"}\n', b""]
        self.replies = []

    def read(self):
        return self.chunks.pop(0)

    def write(self, data) -> None:
        self.replies.append(json.loads(data))

    def close(self) -> None:
        pass


@pytest.fixture(params=sorted(ENDPOINTS))
def bridge_env(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    near, far = ENDPOINTS[request.param]
    bridge = load("unimatrix-bridge.py", "unimatrix_bridge_outbound_reply")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(bridge, "SESSIONS", str(sessions))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    bridge.cfg = types.SimpleNamespace(endpoint=near, key="k", remote="lan", tunnel_remote=None, port=None)
    bridge.log = open(tmp_path / "b.log", "a", encoding="utf-8")
    monkeypatch.setattr(bridge, "notify_sender", lambda line, target, error: None)
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: pytest.fail(f"ssh ran before custody: {argv}"))

    def write_mirror(**fields) -> None:
        row = {"pid": os.getpid(), "name": "far", "messagingSocketPath": near, "macSocketPath": far,
               "unimatrixRemote": "lan", "unimatrixFarName": "far", "sessionId": "sid-far", **fields}
        (sessions / f"{os.getpid()}.json").write_text(json.dumps(row))

    yield bridge, write_mirror, sessions
    bridge.log.close()


def refusal(bridge) -> dict:
    conn = Conn()
    bridge.relay_send(conn)
    assert len(conn.replies) == 1 and conn.replies[0]["ok"] is False
    return conn.replies[0]


def test_a_send_through_a_mirror_whose_name_disagrees_with_its_far_name_is_refused_with_a_reason(bridge_env) -> None:
    bridge, write_mirror, _ = bridge_env
    write_mirror(name="group-em-54.machine-a", unimatrixFarName="example-game-repo-fc")
    reply = refusal(bridge)
    assert reply["error"] and reply.get("reason") in REASON.REASONS


def test_a_send_through_a_mirror_lacking_its_far_session_is_refused_with_a_reason(bridge_env) -> None:
    bridge, write_mirror, _ = bridge_env
    write_mirror(sessionId="")
    reply = refusal(bridge)
    assert reply["error"] and reply.get("reason") in REASON.REASONS


def test_a_send_with_no_mirror_for_this_bridge_is_refused_with_a_reason(bridge_env) -> None:
    bridge, _, _ = bridge_env
    reply = refusal(bridge)
    assert reply["error"] and reply.get("reason") in REASON.REASONS


@pytest.fixture(params=sorted(ENDPOINTS))
def roster(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mod = load("unimatrix-roster.py", "unimatrix_roster_outbound_reply")
    sess = tmp_path / "sessions"
    sess.mkdir()
    monkeypatch.setattr(mod, "sessions_dir", lambda: str(sess))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGY_BRAIN_DIR", str(tmp_path / "agy" / "brain"))
    mod.endpoint = ENDPOINTS[request.param][0]
    mod.home = tmp_path / "home"
    return mod


def verdict(roster, row):
    """('withdrawn', None) when the listing omits `row`; else ('marked'|'unmarked', identity) at default arguments."""
    if not roster.listed_row(row, [row]):
        return "withdrawn", None
    ident = roster.session_identity(row)
    marked = ident.get("canReceive") is False and ident.get("receiveReason") in REASON.REASONS
    return ("marked" if marked else "unmarked"), ident


def face(roster, vendor, **extra) -> dict:
    me = os.getpid()
    return {"vendor": vendor, "pid": me, "procStart": roster.proc_start(me), "messagingSocketPath": roster.endpoint,
            "startedAt": int(time.time() * 1000), **extra}


def test_an_agy_face_with_no_leg_listening_is_marked_or_withdrawn(roster) -> None:
    base = roster.home / "unimatrix"
    (base / "agy-legs").mkdir(parents=True)
    name = "agy-conv-1"
    (base / "peers.json").write_text(json.dumps([{"name": name, "vendor": "agy", "target": "conv-1"}]))
    (base / "agy-legs" / "conv-1.json").write_text(json.dumps({"conversation": "conv-1", "agy_pid": os.getpid()}))
    row = face(roster, "agy", name=name, unimatrixRouterPid=os.getpid())
    kind, ident = verdict(roster, row)
    assert kind in ("withdrawn", "marked"), f"listed with no receive mark: {ident}"


def test_a_transport_face_is_not_listed_as_a_peer(roster) -> None:
    row = face(roster, "transport", name="unimatrix-transport-166e52ca09-on-mac-mini", unimatrixTransport=True)
    assert verdict(roster, row)[0] == "withdrawn"


def test_a_live_claude_row_is_listed_and_never_marked_unable_to_receive(roster) -> None:
    row = face(roster, "claude", name="group-em-54", version="2.1.0")
    kind, ident = verdict(roster, row)
    assert kind == "unmarked" and ident.get("canReceive") is not False
