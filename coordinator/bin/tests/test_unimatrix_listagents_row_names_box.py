"""Every sessions-registry row a bridged peer gets, pushed or pulled, is named `<bare>.<sender box label>` and stamps unimatrixBox."""
import importlib.util
import json
import re
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
SID = "a0b7a784-dbf2-4364-979e-437e652207b1"
CLIENT = "10.0.0.9"
FAR_SOCK = "/tmp/cc-socks/far-c.sock"


def load(mod_name, file):
    spec = importlib.util.spec_from_file_location(mod_name, BIN / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


relay = load("relay_listagents_names_box_t", "unimatrix-relay.py")
roster = load("roster_listagents_names_box_t", "unimatrix-roster.py")

NEAR_ROW = {"unimatrixRemote": "example-operator@machine-a-lan", "unimatrixBox": "machine-a", "unimatrixRemoteIp": CLIENT}
RAW = {"name": "group-em-54", "sessionId": SID, "cwd": "/x", "pid": 1, "messagingSocketPath": "/near/s.sock"}


class FakeProc:
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        return None


def listed(sessions):
    """The rows ListAgents reads: every json file in the sessions registry directory."""
    return [json.loads(p.read_text()) for p in sorted(Path(sessions).glob("*.json"))]


def assert_names_box(row, bare, label, box):
    assert row["name"] == f"{bare}.{label}"
    assert re.fullmatch(r"[^.]+\.[^.]+", row["name"])
    assert row["unimatrixBox"] == box


@pytest.fixture
def pushed(tmp_path, monkeypatch):
    scratch = Path(__file__).resolve().parents[3] / "scratch"
    scratch.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="lb", dir=scratch))
    sess, socks = tmp_path / "sess", root / "s"
    sess.mkdir()

    class Proc:
        stdout = "Thu Nov 12 13:00:00 2026"

    class Helpers:
        local_peer_key = staticmethod(lambda body, ps, domain: f"{body}|{ps}|{domain}")
        write_peer_key = staticmethod(lambda path, value: Path(path).write_text(value, encoding="utf-8"))

    monkeypatch.setattr(relay, "SESS", str(sess))
    monkeypatch.setattr(relay, "SOCKS", str(socks))
    monkeypatch.setattr(relay, "client_address", lambda: CLIENT)
    monkeypatch.setattr(relay, "entries", lambda: iter([(7, dict(NEAR_ROW), "k")]))
    monkeypatch.setattr(relay, "check_name_free", lambda name, client="", own_pid=None: [])
    monkeypatch.setattr(relay, "load_roster", lambda: Helpers)
    monkeypatch.setattr(relay.subprocess, "run", lambda *a, **k: Proc())
    yield sess
    shutil.rmtree(root, ignore_errors=True)


needs_unix = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="AF_UNIX required")


@needs_unix
def test_a_pushed_registration_writes_a_listagents_row_naming_the_sender_box(pushed):
    m = relay.register_name("group-em-54", dict(RAW), "KEY")
    try:
        (row,) = listed(pushed)
        assert_names_box(row, "group-em-54", "machine-a-lan", "machine-a")
        assert row["sessionId"] == SID
    finally:
        m.release()


@needs_unix
def test_a_re_mirrored_labelled_name_registers_with_one_label_not_two(pushed):
    raw = {**RAW, "name": "group-em-54.macmini"}
    m = relay.register_name("group-em-54.macmini", raw, "KEY")
    try:
        (row,) = listed(pushed)
        assert_names_box(row, "group-em-54", "machine-a-lan", "machine-a")
    finally:
        m.release()


@needs_unix
def test_register_name_refuses_a_payload_naming_another_session(pushed):
    with pytest.raises(SystemExit):
        relay.register_name("group-em-54", {**RAW, "name": "example-retrieval-repo-95"}, "KEY")
    assert listed(pushed) == []


def test_push_row_name_and_box_come_from_the_authenticated_client_for_every_session(monkeypatch):
    monkeypatch.setattr(relay, "entries", lambda: iter([(7, dict(NEAR_ROW), "k")]))
    for bare in ("group-em-54", "coordinator-content-repo-73", "example-retrieval-repo-95"):
        row = relay.mirror_entry({**RAW, "name": bare}, bare, *relay.client_box(CLIENT))
        assert_names_box(row, bare, "machine-a-lan", "machine-a")
        row = relay.mirror_entry({**RAW, "name": bare, "unimatrixBox": "forged"}, bare, *relay.client_box(CLIENT))
        assert row["unimatrixBox"] == "machine-a"


def test_pull_row_names_the_sender_box_and_stamps_unimatrixbox(tmp_path, monkeypatch):
    name = "group-em-54"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    ep = str(tmp_path / "cc-msg-x.sock")
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(sessions))
    monkeypatch.setattr(roster, "spawn", lambda cmd: FakeProc(31337))
    monkeypatch.setattr(roster, "proc_start", lambda pid, platform=None: "1")
    monkeypatch.setattr(roster, "remote_ip", lambda remote: CLIENT)
    monkeypatch.setattr(roster.time, "sleep", lambda s: None)
    monkeypatch.setattr(roster, "local_endpoint", lambda sock, platform=None: ep)
    monkeypatch.setattr(roster, "_is_win", lambda platform=None: False)
    cfg = roster.parse_args(["bridge.log", "--remote", "example-operator@machine-a-lan", "--key", "k"])
    entry = {"pid": 7, "name": name, "sessionId": SID, "procStart": "x", "pidDomain": "darwin",
             "messagingSocketPath": FAR_SOCK}
    roster.mirror({"entry": entry, "key": "K"}, cfg, lambda m: None)
    (row,) = listed(sessions)
    assert_names_box(row, "group-em-54", "machine-a-lan", roster._contract().box_id("example-operator@machine-a-lan"))
    assert row["sessionId"] == SID


def test_push_and_pull_name_one_session_identically(monkeypatch):
    monkeypatch.setattr(relay, "entries", lambda: iter([(7, dict(NEAR_ROW), "k")]))
    pushed_name = relay.mirror_entry(RAW, "group-em-54", *relay.client_box(CLIENT))["name"]
    assert pushed_name == roster.labelled("group-em-54", "example-operator@machine-a-lan")
