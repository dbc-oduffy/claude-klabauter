"""Every local writer authenticates for its recipient, including cross-box return delivery."""
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

BIN = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location("local_auth_" + name, BIN / f"unimatrix-{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("json_key", [True, False])
def test_recipient_key_replaces_far_auth_header(tmp_path, monkeypatch, json_key):
    roster = load("roster")
    endpoint = "local-endpoint"
    pid = os.getpid()
    entry = {"pid": pid, "messagingSocketPath": endpoint}
    (tmp_path / f"{pid}.json").write_text(json.dumps(entry))
    key = tmp_path / roster.key_name(pid, endpoint)
    token = "local-test-token"
    key.write_text(json.dumps({"peerToken": token}) if json_key else token)
    data = b'{"type":"auth","token":"far-stale-token"}\n{"type":"user"}\n'
    authenticated = roster.authenticated_data(endpoint, data, tmp_path)
    lines = authenticated.splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"type": "auth", "token": token}
    assert json.loads(lines[1]) == {"type": "user"}
    monkeypatch.setattr(roster, "pid_alive", lambda pid: False)
    assert roster.authenticated_data(endpoint, data, tmp_path) == data


@pytest.mark.parametrize("writer", ["bridge", "inbox"])
def test_return_writer_uses_local_recipient_auth(writer, tmp_path, monkeypatch):
    module = load(writer)
    roster = load("roster")
    monkeypatch.setattr(roster, "recipient_token", lambda endpoint, directory: "local-test-token")
    monkeypatch.setattr(module, "_roster", lambda: roster)
    monkeypatch.setattr(module, "IS_WIN", True)
    monkeypatch.setattr(module, "SESSIONS", str(tmp_path))
    written, closed = [], []
    win32file = SimpleNamespace(
        GENERIC_READ=1, GENERIC_WRITE=2, OPEN_EXISTING=3,
        CreateFile=lambda *a: "handle",
        WriteFile=lambda handle, data: written.append(data),
        FlushFileBuffers=lambda handle: None,
        CloseHandle=lambda handle: closed.append(handle))
    monkeypatch.setitem(sys.modules, "win32file", win32file)
    module.write_local("local-endpoint", b'{"type":"auth","token":"far-token"}\n{"type":"user"}\n')
    assert json.loads(written[0].splitlines()[0]) == {"type": "auth", "token": "local-test-token"}
    assert len(written[0].splitlines()) == 2
    assert closed == ["handle"]



def test_windows_relay_send_authenticates_for_local_recipient(tmp_path, monkeypatch):
    import io
    relay, roster = load("relay"), load("roster")
    endpoint = r"\\.\pipe\LOCAL\cc-msg-test"
    monkeypatch.setattr(relay, "endpoint_kind", lambda: "pipe")
    monkeypatch.setattr(relay, "entries", lambda: iter([(5, {"messagingSocketPath": endpoint}, "5.key")]))
    monkeypatch.setattr(relay, "load_roster", lambda: roster)
    monkeypatch.setattr(roster, "recipient_token", lambda endpoint, directory: "local-test-token")
    monkeypatch.setattr(relay, "local_reply_address", lambda data: data)
    monkeypatch.setattr(relay.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b'{"type":"user"}\n')))
    written, closed = [], []
    monkeypatch.setitem(sys.modules, "win32file", SimpleNamespace(
        GENERIC_READ=1, GENERIC_WRITE=2, OPEN_EXISTING=3,
        CreateFile=lambda *a: "handle", WriteFile=lambda handle, data: written.append(data),
        FlushFileBuffers=lambda handle: None,
        CloseHandle=lambda handle: closed.append(handle)))
    relay.send(5)
    assert json.loads(written[0].splitlines()[0]) == {"type": "auth", "token": "local-test-token"}
    assert json.loads(written[0].splitlines()[1]) == {"type": "user"}
    assert closed == ["handle"]



@pytest.mark.parametrize("platform,field", [("win32", "procStartFt"), ("darwin", "procStart"), ("linux", "procStart")])
@pytest.mark.parametrize("raw", ["a" * 32, "b" * 64, '{"peerToken":"cccccccccccccccccccccccccccccccc","procStart":"far"}'])
def test_local_peer_key_uses_native_token_and_local_identity(platform, field, raw):
    import re
    roster = load("roster")
    key = roster.local_peer_key(raw, "local-process-start", "local-domain", platform)
    assert re.fullmatch(r"[0-9a-f]{32}", key["peerToken"])
    assert key[field] == "local-process-start"
    assert key["pidDomain"] == "local-domain"
    assert len(key) == 3
    if raw == "a" * 32:
        assert key["peerToken"] == raw
    elif raw.startswith("{"):
        assert key["peerToken"] == "c" * 32


def test_live_mirror_key_repairs_remote_envelope_and_keeps_local_token(tmp_path, monkeypatch):
    roster = load("roster")
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(tmp_path))
    entry = {"pid": 1234, "messagingSocketPath": "local-pipe", "procStart": "local-start", "pidDomain": "local-domain"}
    path = tmp_path / roster.key_name(1234, "local-pipe")
    path.write_text(json.dumps({"peerToken": "a" * 32, "procStart": "remote-start", "pidDomain": "far-domain"}))
    roster.refresh_mirror_key(entry, "ignored-far-token")
    key = json.loads(path.read_text())
    field = "procStartFt" if sys.platform == "win32" else "procStart"
    assert key == {"peerToken": "a" * 32, field: "local-start", "pidDomain": "local-domain"}
    monkeypatch.setattr(roster, "write_peer_key", lambda *a: pytest.fail("rewrote valid local key"))
    roster.refresh_mirror_key(entry, "new-far-token")
