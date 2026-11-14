"""Explicit temporary reply endpoints export once; ordinary mirrors remain private."""
import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


BIN = Path(__file__).resolve().parents[1]


def _load(filename, name):
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


relay = _load("unimatrix-relay.py", "reply_inbox_relay")
roster = _load("unimatrix-roster.py", "reply_inbox_roster")
client = _load("unimatrix-inbox.py", "reply_inbox_client")


def test_reply_inbox_is_an_explicit_strict_mode_and_client_option(tmp_path):
    name = "codex-Rag_c8-on-MacMini"
    assert relay.parse_mode("reply-inbox " + name) == ("reply-inbox", name)
    for bad in ("../escape", "name@box", "x;id", "two names", "\nname", "name "):
        assert relay.parse_mode("reply-inbox " + bad) is None
    args = ["1", "codex-rag-c8", str(tmp_path / "log"), "--remote", "macmini"]
    assert not client.parse_args(args).reply_inbox
    assert client.parse_args(args + ["--reply-inbox"]).reply_inbox


def test_registration_payload_cannot_opt_itself_into_export():
    raw = {"cwd": "/work/repo", "unimatrixReplyInbox": True, "unimatrixMirror": True,
           "unimatrixClient": "forged", "vendor": "transport", "box": "forged"}
    assert relay.mirror_entry(raw, "temporary") == {"name": "temporary", "cwd": "/work/repo"}


@pytest.mark.parametrize("mode,expected", [("inbox", False), ("reply-inbox", True)])
def test_relay_mode_alone_controls_reply_export(monkeypatch, mode, expected):
    captured = []
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", mode + " temporary")
    monkeypatch.setattr(relay, "inbox", lambda name, export_reply=False: captured.append((name, export_reply)))
    relay.main()
    assert captured == [("temporary", expected)]


@pytest.mark.parametrize("opt_in", [False, True])
def test_client_requests_only_the_explicitly_selected_mode(tmp_path, monkeypatch, opt_in):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "1.json").write_text('{"pid":1}')
    (sessions / "1.test.key").write_text("test-token")
    commands = []
    process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO(),
                              pid=123, wait=lambda: 0)
    monkeypatch.setattr(client, "SESSIONS", str(sessions))
    monkeypatch.setattr(client, "pick_remote", lambda *args: "macmini")
    monkeypatch.setattr(client.subprocess, "Popen", lambda command, **kwargs: commands.append(command) or process)
    arguments = ["1", "temporary", str(tmp_path / "log"), "--remote", "macmini"]
    try:
        client.main(arguments + (["--reply-inbox"] if opt_in else []))
    finally:
        client.log.close()
    assert commands[0][-1] == ("reply-inbox temporary" if opt_in else "inbox temporary")


def relay_box(host):
    """The expected box id of a hostname spelling."""
    return host.partition(".")[0].lower()


def _exported(tmp_path, monkeypatch, capsys, host="Hosted-Box"):
    source = {"pid": 2, "name": "temporary", "unimatrixMirror": True, "unimatrixReplyInbox": True,
              "unimatrixRemoteSocket": "/client.sock", "unimatrixClient": "192.0.2.10",
              "messagingSocketPath": "/hosted.sock", "cwd": "/work/repo"}
    downstream = {**source, "pid": 3, "name": "downstream", "macSocketPath": "/hosted.sock"}
    ordinary = {"pid": 4, "name": "ordinary", "unimatrixMirror": True}
    key = tmp_path / "test.key"
    key.write_text("test-token")
    monkeypatch.setattr(relay, "SESS", str(tmp_path))
    monkeypatch.setattr(relay, "entries", lambda: iter([(2, source, key.name), (3, downstream, key.name), (4, ordinary, key.name)]))
    monkeypatch.setattr(relay, "engine_fields", lambda: {})
    monkeypatch.setattr(relay, "load_roster", lambda: roster)
    monkeypatch.setattr(relay.socket, "gethostname", lambda: host)
    relay.roster()
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(rows) == 1
    exported = rows[0]["entry"]
    assert exported["name"] == "temporary" and exported["vendor"] == "transport"
    assert exported["box"] == relay_box(host) and exported["unimatrixReplyInbox"] is True
    assert not {"unimatrixMirror", "unimatrixRemoteSocket", "unimatrixClient"} & set(exported)
    assert source["unimatrixMirror"] is True  # publication changes a copy, never local ownership
    return rows[0]


def test_reply_endpoint_exports_once_without_mirror_markers(tmp_path, monkeypatch, capsys):
    _exported(tmp_path, monkeypatch, capsys)


@pytest.mark.parametrize("host", ["Machine-b.local", "machine-b", "MACHINE-B.Local"])
def test_reply_endpoint_box_is_one_id_across_hostname_spellings(tmp_path, monkeypatch, capsys, host):
    assert _exported(tmp_path, monkeypatch, capsys, host)["entry"]["box"] == "machine-b"


def test_downstream_reply_mirror_survives_repeated_sync_without_echo(tmp_path, monkeypatch, capsys):
    row = _exported(tmp_path, monkeypatch, capsys)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    calls = []
    process = SimpleNamespace(pid=31337, poll=lambda: None)
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(sessions))
    monkeypatch.setattr(roster, "spawn", lambda command: calls.append(command) or process)
    monkeypatch.setattr(roster, "proc_start", lambda pid: "local-start")
    monkeypatch.setattr(roster, "pid_domain", lambda: "win32:near")
    monkeypatch.setattr(roster, "pid_alive", lambda pid: True)
    monkeypatch.setattr(roster.time, "sleep", lambda _: None)
    monkeypatch.setattr(roster, "local_endpoint", lambda seed: str(tmp_path / "local.sock"))
    monkeypatch.setattr(roster, "write_peer_key", lambda *args: None)
    monkeypatch.setattr(roster, "local_peer_key", lambda *args: {})
    cfg = SimpleNamespace(remote="hosted-box", key="unused", tunnel_remote=None, log=str(tmp_path / "log"))
    bridges = {}
    for _ in range(3):
        assert roster.inbound_names() == set()
        rows = roster.mirrorable([row], set(), roster.inbound_names())
        assert set(rows) == {"/hosted.sock"}
        roster.sync(bridges, rows, cfg, lambda _: None)
    assert len(calls) == 1
    downstream = json.loads((sessions / "31337.json").read_text())
    assert downstream["macSocketPath"] == "/hosted.sock"
    assert not downstream.get("unimatrixMirror")
    monkeypatch.setattr(relay, "SESS", str(sessions))
    monkeypatch.setattr(relay, "entries", lambda: iter([(31337, downstream, "nonexistent.key")]))
    relay.roster()
    assert capsys.readouterr().out == ""  # downstream is excluded before even reading its key


@pytest.mark.parametrize("export_reply", [False, True])
def test_reply_opt_in_keeps_existing_ssh_eof_cleanup(tmp_path, monkeypatch, export_reply):
    sessions, sockets = tmp_path / "sessions", tmp_path / "sockets"
    sessions.mkdir()
    sockets.mkdir()

    class Input(io.StringIO):
        def fileno(self):
            return 0

    class Socket:
        def bind(self, path):
            Path(path).touch()

        def listen(self, backlog):
            pass

    pid = os.getpid()
    captured = []
    monkeypatch.setattr(relay, "SESS", str(sessions))
    monkeypatch.setattr(relay, "SOCKS", str(sockets))
    monkeypatch.setattr(relay, "endpoint_kind", lambda: "socket")
    monkeypatch.setattr(relay, "check_name_free", lambda *args: [])
    monkeypatch.setattr(relay, "client_address", lambda: "192.0.2.10")
    monkeypatch.setattr(relay.socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(relay.socket, "socket", lambda *args: Socket())
    monkeypatch.setattr(relay.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="start"))
    monkeypatch.setattr(relay.sys, "stdin", Input('{"unimatrixReplyInbox":true}\n' + "a" * 32 + "\n"))
    monkeypatch.setattr(relay, "serve", lambda *args: captured.append(json.loads((sessions / f"{pid}.json").read_text())))
    relay.inbox("temporary", export_reply=export_reply)
    assert bool(captured[0].get("unimatrixReplyInbox")) is export_reply
    assert list(sessions.iterdir()) == [] and list(sockets.iterdir()) == []
