"""Tests for the POSIX control-socket transport (AF_UNIX, peer attestation, NDJSON)."""
import importlib.util
import json
import os
import socket
import stat
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX transport")

_LIB = Path(__file__).resolve().parents[1] / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from claude_author_control import protocol, transport_posix  # noqa: E402


@pytest.fixture
def short_dir():
    """sun_path caps near 104 bytes, which pytest's tmp_path exceeds on macOS."""
    import shutil
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="cat", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def transport(short_dir):
    d = short_dir / "i"
    t = transport_posix.PosixTransport(str(d))

    def handler(req, peer):
        return protocol.ok_response(
            req.id, identity=protocol.identity_for(peer, req.on_behalf_of),
            status={"alive": True})

    t.serve(handler)
    yield t
    t.close()


def _client(t):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(0.05)
    c.connect(t.address)
    return c


def _roundtrip(t, c, payload: bytes):
    c.sendall(payload)
    buf = b""
    deadline = time.time() + 3
    while b"\n" not in buf and time.time() < deadline:
        t.poll(0.05)
        try:
            chunk = c.recv(65536)
        except socket.timeout:
            continue
        if not chunk:
            break
        buf += chunk
    return buf


def _status(i=1):
    return json.dumps({"v": 0, "id": i, "op": "status"}).encode() + b"\n"


def test_status_roundtrip_same_uid(transport):
    c = _client(transport)
    resp = json.loads(_roundtrip(transport, c, _status()))
    assert resp["ok"] is True and resp["id"] == 1
    assert resp["identity"]["uid"] == os.geteuid()
    assert resp["identity"]["transport"] == "unix"
    c.close()


def test_malformed_line_bad_args_null_id(transport):
    c = _client(transport)
    resp = json.loads(_roundtrip(transport, c, b"not json\n"))
    assert resp["ok"] is False and resp["id"] is None
    assert resp["error"]["code"] == "bad_args"
    # connection survives a malformed line
    assert json.loads(_roundtrip(transport, c, _status(2)))["id"] == 2
    c.close()


def test_two_clients_concurrently(transport):
    a, b = _client(transport), _client(transport)
    a.sendall(_status(1))
    b.sendall(_status(2))
    got = {}
    deadline = time.time() + 3
    while len(got) < 2 and time.time() < deadline:
        transport.poll(0.05)
        for name, c in (("a", a), ("b", b)):
            try:
                data = c.recv(65536)
            except socket.timeout:
                continue
            if data:
                got[name] = json.loads(data)
    assert got["a"]["id"] == 1 and got["b"]["id"] == 2
    a.close()
    b.close()


def test_over_length_line_closes_connection(transport):
    c = _client(transport)
    c.setblocking(False)
    pending = memoryview(b"x" * (protocol.MAX_FRAME_BYTES + 10))
    deadline = time.time() + 5
    data = b""
    closed = False
    while time.time() < deadline and not closed:
        if pending:
            try:
                pending = pending[c.send(pending):]
            except (BlockingIOError, InterruptedError):
                pass
            except OSError:
                pending = pending[len(pending):]
        transport.poll(0.01)
        try:
            chunk = c.recv(65536)
        except (BlockingIOError, socket.timeout):
            continue
        except ConnectionResetError:
            closed = True
            break
        if chunk == b"":
            closed = True
        else:
            data += chunk
    assert closed
    if data:
        assert json.loads(data.split(b"\n")[0])["error"]["code"] == "bad_args"
    assert not transport._conns


def test_socket_and_dir_modes(transport):
    assert stat.S_IMODE(os.stat(transport.address).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(transport.instance_dir).st_mode) == 0o700


def test_close_unlinks_socket(short_dir):
    t = transport_posix.PosixTransport(str(short_dir / "i"))
    t.serve(lambda r, p: protocol.ok_response(r.id))
    assert os.path.exists(t.address)
    t.close()
    assert not os.path.exists(t.address)


def test_uid_mismatch_gets_not_attested(transport, monkeypatch):
    monkeypatch.setattr(transport_posix, "read_peer_credentials",
                        lambda s: (os.geteuid() + 1, 1234))
    c = _client(transport)
    resp = json.loads(_roundtrip(transport, c, _status()))
    assert resp["error"]["code"] == "not_attested"
    c.close()


def test_unreadable_credentials_get_not_attested(transport, monkeypatch):
    monkeypatch.setattr(transport_posix, "read_peer_credentials", lambda s: None)
    c = _client(transport)
    resp = json.loads(_roundtrip(transport, c, _status()))
    assert resp["error"]["code"] == "not_attested"
    c.close()


def test_real_credentials_read(transport):
    c = _client(transport)
    transport.poll(0.1)
    creds = transport_posix.read_peer_credentials(c)
    assert creds is not None and creds[0] == os.geteuid()
    c.close()


def test_long_path_refused(tmp_path):
    t = transport_posix.PosixTransport(str(tmp_path / ("d" * 120)))
    with pytest.raises(ValueError):
        t.serve(lambda r, p: {})
    assert not os.path.exists(t.instance_dir)
