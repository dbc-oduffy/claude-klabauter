"""`_session_path` accepts a live session's socket under the Linux runtime dir, not only /tmp/cc-socks."""
import importlib.util
import os
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    spec = importlib.util.spec_from_file_location("unimatrix_relay_session_socks", BIN / "unimatrix-relay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "endpoint_kind", lambda platform=None: "socket")
    monkeypatch.setattr(mod.stat, "S_ISSOCK", lambda mode: True)
    return mod


def endpoint(relay, monkeypatch, path):
    monkeypatch.setattr(relay, "entries", lambda: iter([(7, {"name": "n", "messagingSocketPath": str(path)}, "k")]))
    monkeypatch.setattr(relay, "record_delivery", lambda row: None)


def test_xdg_runtime_dir_socket_is_a_session_socket(relay, tmp_path, monkeypatch):
    sock = tmp_path / "run" / "user" / "1000" / "cc-socks" / "18733.sock"
    sock.parent.mkdir(parents=True)
    sock.write_text("")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(sock.parent.parent))
    endpoint(relay, monkeypatch, sock)
    assert relay._session_path(7) == str(sock)


def test_no_xdg_falls_back_to_run_user_uid(relay, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
    assert os.path.realpath("/run/user/1000/cc-socks") in relay.session_sock_dirs()
    assert os.path.realpath(relay.SOCKS) in relay.session_sock_dirs()


def test_a_foreign_directory_is_still_refused(relay, tmp_path, monkeypatch):
    sock = tmp_path / "elsewhere" / "x.sock"
    sock.parent.mkdir()
    sock.write_text("")
    endpoint(relay, monkeypatch, sock)
    with pytest.raises(SystemExit, match="non-session socket"):
        relay._session_path(7)
