"""A unix-socket delivery carries the recipient's auth header, as a pipe delivery does.

A POSIX router face refuses a frame with no token ("sender side: no token presented"); the Mini agy
relay-in failed exactly so while the face's key file sat on disk. Hermetic: no socket is opened.
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

BIN = Path(__file__).resolve().parents[1]
HEADER = b'{"type": "auth", "token": "t0k"}\n'


def _relay(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    spec = importlib.util.spec_from_file_location("unimatrix_relay_posix_auth", BIN / "unimatrix-relay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_unix_socket_send_writes_the_auth_header_before_the_frame(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    sent = []

    class FakeSocket:
        def __init__(self, *a):
            pass

        def connect(self, path):
            pass

        def sendall(self, data):
            sent.append(data)

        def close(self):
            pass

    monkeypatch.setattr(relay, "endpoint_kind", lambda platform=None: "socket")
    monkeypatch.setattr(relay, "local_reply_address", lambda line: line)
    monkeypatch.setattr(relay, "load_roster", lambda: SimpleNamespace(
        authenticated_data=lambda p, line, s: HEADER + line))
    monkeypatch.setattr(relay, "_socket_peer_pid", lambda s: None)
    monkeypatch.setattr(relay, "_confirm_server", lambda pid: None)
    monkeypatch.setattr(relay.socket, "socket", FakeSocket, raising=False)
    monkeypatch.setattr(relay.socket, "AF_UNIX", 1, raising=False)
    relay._REACHED.clear()
    relay._write_line("/tmp/cc-socks/face.sock", b'{"text": "hi"}\n')
    assert sent == [HEADER + b'{"text": "hi"}\n']
