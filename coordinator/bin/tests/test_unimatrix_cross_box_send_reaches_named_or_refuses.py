"""A cross-box send writes only to the endpoint served by the addressed pid, else refuses; notices and receipts name the box."""
import importlib.util
import io
import json
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
SID = "11111111-2222-3333-4444-555555555555"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    mod = load("unimatrix_relay_named_or_refuses", "unimatrix-relay.py")
    monkeypatch.setattr(mod, "_session_path", lambda *a, **k: (mod._REACHED.update(pid=7, name="far", session=SID), "/tmp/cc-socks/x.sock")[1])
    monkeypatch.setattr(mod, "local_box", lambda: "machine-a")
    monkeypatch.setattr(mod, "local_reply_address", lambda line: line)
    monkeypatch.setattr(mod, "endpoint_kind", lambda platform=None: "socket")
    return mod


def fake_socket_module(relay, monkeypatch, peer_pid, sent):
    class Sock:
        def __init__(self, *a):
            pass

        def connect(self, path):
            pass

        def sendall(self, data):
            sent.append(data)

        def close(self):
            pass

    monkeypatch.setattr(relay.socket, "socket", Sock)
    monkeypatch.setattr(relay.socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(relay, "_socket_peer_pid", lambda s: peer_pid)


def run(relay, monkeypatch, capsys):
    class Stdin:
        buffer = io.BytesIO(b'{"m":"x"}\n')

    monkeypatch.setattr(relay.sys, "stdin", Stdin)
    relay.send_batch(7, "d" * 16, "e" * 16, SID)
    return [json.loads(x) for x in capsys.readouterr().out.splitlines()]


def test_an_endpoint_served_by_another_pid_refuses_and_writes_nothing(relay, monkeypatch, capsys):
    sent = []
    fake_socket_module(relay, monkeypatch, 999, sent)
    (reply,) = run(relay, monkeypatch, capsys)
    assert reply["ok"] is False and reply["error"].startswith("target mismatch")
    assert "999" in reply["error"] and "pid 7" in reply["error"]
    assert sent == []
    assert not relay.receipts_path().exists()


def test_an_endpoint_served_by_the_addressed_pid_delivers_with_the_box_named(relay, monkeypatch, capsys):
    sent = []
    fake_socket_module(relay, monkeypatch, 7, sent)
    (reply,) = run(relay, monkeypatch, capsys)
    assert reply["ok"] is True and reply["reached_box"] == "machine-a" and sent == [b'{"m":"x"}\n']
    (row,) = [json.loads(x) for x in relay.receipts_path().read_text(encoding="utf-8").splitlines()]
    assert row["box"] == "machine-a" and row["session"] == SID


def test_an_unanswerable_peer_query_refuses_nothing(relay, monkeypatch, capsys):
    sent = []
    fake_socket_module(relay, monkeypatch, None, sent)
    (reply,) = run(relay, monkeypatch, capsys)
    assert reply["ok"] is True and len(sent) == 1


def test_the_refusal_row_in_the_delivery_record_names_the_box(relay, monkeypatch, capsys):
    fake_socket_module(relay, monkeypatch, 999, [])
    run(relay, monkeypatch, capsys)
    (row,) = [json.loads(x) for x in relay.deliveries_path().read_text(encoding="utf-8").splitlines()]
    assert row["ok"] is False and row["box"] == "machine-a"


def test_the_pipe_server_pid_is_confirmed_on_the_handle_before_the_write(relay, monkeypatch, capsys):
    import sys as _sys
    written = []
    fake = types.SimpleNamespace(
        GENERIC_READ=1, GENERIC_WRITE=2, OPEN_EXISTING=3, CreateFile=lambda *a: "h",
        WriteFile=lambda h, line: written.append(line), FlushFileBuffers=lambda h: None, CloseHandle=lambda h: None)
    monkeypatch.setitem(_sys.modules, "win32file", fake)
    monkeypatch.setattr(relay, "endpoint_kind", lambda platform=None: "pipe")
    monkeypatch.setattr(relay, "load_roster", lambda: types.SimpleNamespace(authenticated_data=lambda p, l, s: l))
    monkeypatch.setattr(relay, "record_receipt", lambda sid, line: "digest")
    monkeypatch.setattr(relay, "_pipe_server_pid", lambda h: 4242)
    (reply,) = run(relay, monkeypatch, capsys)
    assert reply["ok"] is False and reply["error"].startswith("target mismatch") and written == []
    monkeypatch.setattr(relay, "_pipe_server_pid", lambda h: 7)
    (reply,) = run(relay, monkeypatch, capsys)
    assert reply["ok"] is True and len(written) == 1


def test_a_target_mismatch_with_no_resolved_pid_is_not_enforced(relay):
    relay._REACHED.clear()
    relay._confirm_server(12345)


@pytest.fixture
def bridge():
    return load("unimatrix_bridge_named_or_refuses", "unimatrix-bridge.py")


@pytest.mark.parametrize("box, expect", [("machine-a", "far (pid 1, session s) on box machine-a"), ("unknown", "far (pid 1, session s)")])
def test_the_failure_notice_names_the_target_box(bridge, monkeypatch, box, expect):
    notices = []
    monkeypatch.setattr(bridge, "record_delivery", lambda row: None)
    monkeypatch.setattr(bridge, "notify_sender", lambda line, target, error: notices.append(target))
    bridge._record_outcome(b"{}", "far (pid 1, session s)", "t", 1, "boom", box, "ssh")
    assert notices == [expect]


def test_the_relay_refusal_text_carries_the_prefix_the_bridge_unmirrors_on(relay, bridge):
    with pytest.raises(relay.TargetMismatch) as refused:
        relay._REACHED.update(pid=7)
        relay._confirm_server(999)
    assert str(refused.value).startswith(bridge.TARGET_MISMATCH)


@pytest.mark.parametrize("reply_error, marked", [
    ("target mismatch: endpoint is served by pid 999, not addressed pid 4242", True),
    ("ConnectionRefusedError: gone", False),
])
def test_a_relay_reply_refusal_marks_the_mirror_only_for_a_target_mismatch(bridge, tmp_path, monkeypatch, reply_error, marked):
    import os
    import subprocess

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "messagingSocketPath": "/tmp/cc-socks/mirror.sock", "macSocketPath": "/far/4242.sock",
         "name": "far-session", "unimatrixFarName": "far-session", "sessionId": SID}))
    monkeypatch.setattr(bridge, "SESSIONS", str(sessions))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setattr(bridge._box_mux(), "IS_WIN", False)
    bridge.cfg = types.SimpleNamespace(endpoint="/tmp/cc-socks/mirror.sock", key="k", remote="farbox", port=None)
    bridge.log = open(tmp_path / "bridge.log", "a", encoding="utf-8")
    monkeypatch.setattr(bridge, "write_local", lambda ep, data: None)
    reply = json.dumps({"ok": False, "error": reply_error}).encode() + b"\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, reply, b""))

    class Conn:
        chunks = [json.dumps({"from": "uds:/tmp/cc-socks/s.sock", "message": "hi"}).encode() + b"\n", b""]

        def read(self):
            return self.chunks.pop(0)

        def close(self):
            pass

    bridge.relay_send(Conn())
    assert (sessions / f"{os.getpid()}.misroute").exists() is marked


def roster_row(**kw):
    return (1, {"name": "machine-a-face", "unimatrixRemote": "example-operator@machine-a-lan", "unimatrixBox": "machine-a",
                "unimatrixClient": "10.0.0.5", **kw}, "k")


def test_a_pushed_mirror_is_named_for_the_box_its_authenticated_client_belongs_to(relay, monkeypatch):
    monkeypatch.setattr(relay, "entries", lambda: iter([roster_row()]))
    box, label = relay.client_box("10.0.0.5")
    assert (box, label) == ("machine-a", "machine-a-lan")
    row = relay.mirror_entry({"sessionId": SID, "cwd": "/x"}, "group-em-54", box, label)
    assert row["name"] == "group-em-54.machine-a-lan" and row["unimatrixBox"] == "machine-a"
    assert row[relay.MIRROR_NAME_FIELD] == "group-em-54" and row["sessionId"] == SID
    again = relay.mirror_entry({}, "group-em-54.machine-a-lan", box, label)
    assert again["name"] == "group-em-54.machine-a-lan"


def test_a_client_no_roster_row_vouches_for_is_labelled_by_its_address_with_no_box(relay, monkeypatch):
    monkeypatch.setattr(relay, "entries", lambda: iter([roster_row()]))
    assert relay.client_box("10.9.9.9") == (None, "10-9-9-9")
    assert relay.client_box("") == (None, None)
    row = relay.mirror_entry({"unimatrixBox": "forged"}, "group-em-54", None, None)
    assert row["name"] == "group-em-54" and "unimatrixBox" not in row


def run_pinned(relay, monkeypatch, capsys):
    class Stdin:
        buffer = io.BytesIO(b'{"m":"x"}\n')

    monkeypatch.setattr(relay.sys, "stdin", Stdin)
    relay.send_batch(7, relay.socket_digest("/tmp/cc-socks/x.sock"), None, SID)
    return [json.loads(x) for x in capsys.readouterr().out.splitlines()]


@pytest.fixture
def session_relay(tmp_path, monkeypatch):
    """A relay whose real `_session_path` runs against a registry where pid 7 holds one session."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    mod = load("unimatrix_relay_session_pin", "unimatrix-relay.py")
    held = {"name": "example-retrieval-repo-95", "sessionId": "other-session", "messagingSocketPath": "/tmp/cc-socks/x.sock"}
    monkeypatch.setattr(mod, "entries", lambda: iter([(7, held, "7.key")]))
    monkeypatch.setattr(mod, "local_box", lambda: "machine-a")
    monkeypatch.setattr(mod, "local_reply_address", lambda line: line)
    monkeypatch.setattr(mod, "endpoint_kind", lambda platform=None: "socket")
    return mod


def test_a_stand_in_delivers_only_to_the_session_its_row_names(session_relay, monkeypatch, capsys):
    sent = []
    fake_socket_module(session_relay, monkeypatch, 7, sent)
    with pytest.raises(SystemExit) as refused:
        run_pinned(session_relay, monkeypatch, capsys)
    error = str(refused.value)
    assert error.startswith("target mismatch") and "other-session" in error and SID in error
    assert sent == []


def test_a_stand_in_whose_session_is_still_held_delivers(session_relay, monkeypatch, capsys):
    sent = []
    fake_socket_module(session_relay, monkeypatch, 7, sent)
    fake_os = types.SimpleNamespace(**vars(session_relay.os))
    fake_os.stat = lambda p: types.SimpleNamespace(st_mode=0)
    monkeypatch.setattr(session_relay, "os", fake_os)
    monkeypatch.setattr(session_relay, "SOCKS", "/tmp/cc-socks")
    monkeypatch.setattr(session_relay.stat, "S_ISSOCK", lambda mode: True)
    monkeypatch.setattr(session_relay, "entries", lambda: iter([(7, {"name": "group-em-54", "sessionId": SID, "messagingSocketPath": "/tmp/cc-socks/x.sock"}, "7.key")]))
    (reply,) = run_pinned(session_relay, monkeypatch, capsys)
    assert reply["ok"] is True and len(sent) == 1


def test_the_session_the_row_names_is_gone_refuses(session_relay, monkeypatch, capsys):
    sent = []
    fake_socket_module(session_relay, monkeypatch, 7, sent)
    monkeypatch.setattr(session_relay, "entries", lambda: iter([]))
    with pytest.raises(SystemExit) as refused:
        run_pinned(session_relay, monkeypatch, capsys)
    assert "no live session" in str(refused.value) and sent == []


def test_the_failure_notice_leads_with_the_name_box_address_the_far_box_resolves(bridge, monkeypatch):
    notices = []
    monkeypatch.setattr(bridge, "record_delivery", lambda row: None)
    monkeypatch.setattr(bridge, "notify_sender", lambda line, target, error: notices.append(target))
    bridge._record_outcome(b"{}", "far (pid 1, session s)", "t", 1, "boom", "machine-a", "ssh", "far.machine-a")
    assert notices == ["far.machine-a (far (pid 1, session s)) on box machine-a"]


def test_a_receipt_row_quotes_the_reached_session_as_the_name_box_address(relay, tmp_path):
    relay._REACHED.update(pid=7, name="group-em-54.old", session=SID)
    relay.record_receipt(SID, b"hi\n")
    row = json.loads(relay.receipts_path().read_text(encoding="utf-8").splitlines()[-1])
    assert row["ref"] == "group-em-54.machine-a" and row["box"] == "machine-a"
    relay._REACHED.clear()
    relay.record_receipt(SID, b"hi\n")
    assert json.loads(relay.receipts_path().read_text(encoding="utf-8").splitlines()[-1])["ref"] is None


def named_send(relay, monkeypatch, name_for_digest):
    fake_os = types.SimpleNamespace(**vars(relay.os))
    fake_os.stat = lambda p: types.SimpleNamespace(st_mode=0)
    monkeypatch.setattr(relay, "os", fake_os)
    monkeypatch.setattr(relay, "SOCKS", "/tmp/cc-socks")
    monkeypatch.setattr(relay.stat, "S_ISSOCK", lambda mode: True)
    monkeypatch.setattr(relay, "_router_resolve", lambda name: None)
    monkeypatch.setattr(relay, "load_roster", lambda: types.SimpleNamespace(
        session_identity=lambda d: {"name": d["name"]}, authenticated_data=lambda p, l, s: l))

    class Stdin:
        buffer = io.BytesIO(b'{"m":"x"}\n')

    monkeypatch.setattr(relay.sys, "stdin", Stdin)
    relay.send_batch(7, None, relay.name_digest(name_for_digest), None)


def test_send_named_accepts_the_bare_name_dot_own_box_label_and_refuses_another_box(session_relay, monkeypatch, capsys):
    sent = []
    fake_socket_module(session_relay, monkeypatch, 7, sent)
    named_send(session_relay, monkeypatch, "example-retrieval-repo-95.machine-a")
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["ok"] is True and len(sent) == 1
    with pytest.raises(SystemExit) as refused:
        named_send(session_relay, monkeypatch, "example-retrieval-repo-95.mini")
    assert str(refused.value).startswith("target mismatch") and len(sent) == 1


def test_the_stand_in_delivery_passes_its_rows_session_id_to_the_far_relay(bridge, tmp_path, monkeypatch):
    import os

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "messagingSocketPath": "/tmp/cc-socks/mirror.sock", "macSocketPath": "/far/4242.sock",
         "name": "far-session.machine-a", "unimatrixBox": "machine-a", "box": "machine-a", "unimatrixFarName": "far-session",
         "sessionId": SID, "target": None}))
    monkeypatch.setattr(bridge, "SESSIONS", str(sessions))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    bridge.cfg = types.SimpleNamespace(endpoint="/tmp/cc-socks/mirror.sock", key="k", remote="u@machine-a", port=None)
    bridge.log = open(tmp_path / "bridge.log", "a", encoding="utf-8")
    commands, notices = [], []
    monkeypatch.setattr(bridge, "record_delivery", lambda row: None)
    monkeypatch.setattr(bridge, "notify_sender", lambda line, target, error: notices.append(target))

    def fake_ssh(command, data):
        import subprocess
        commands.append(command)
        r = subprocess.CompletedProcess([], 1, b"", b"target mismatch: pid 4242 holds session x, not addressed session s")
        r.transport = "ssh"
        return r

    monkeypatch.setattr(bridge, "_ssh_relay", fake_ssh)

    class Conn:
        chunks = [json.dumps({"from": "uds:/tmp/cc-socks/s.sock", "message": "hi"}).encode() + b"\n", b""]

        def read(self):
            return self.chunks.pop(0)

        def close(self):
            pass

    bridge.relay_send(Conn())
    assert commands and commands[0].startswith("send-session 4242 ") and commands[0].endswith(f" {SID}")
    assert notices and notices[0].startswith("far-session.machine-a (") and notices[0].endswith("on box machine-a")
