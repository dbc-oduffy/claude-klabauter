"""Behavior pins for the unimatrix relay `send-to <pid> <digest>` verb."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

RELAY = Path(__file__).resolve().parents[1] / "unimatrix-relay.py"
PIPE_A = "\\\\.\\pipe\\cc-a"
PIPE_B = "\\\\.\\pipe\\cc-b"


def load():
    spec = importlib.util.spec_from_file_location("unimatrix_relay_send_to", RELAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = load()


def digest(path):
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


class _Stdin:
    def __init__(self, data):
        self.buffer = io.BytesIO(data)


@pytest.fixture
def written(monkeypatch):
    sent = []
    monkeypatch.setattr(R, "_write_line", lambda path, line: sent.append((path, line)))
    return sent


def serve(monkeypatch, kind, path, tmp_path):
    """One live session, pid 7, at `path`; `kind` picks the endpoint family."""
    monkeypatch.setattr(R, "entries", lambda: iter([(7, {"messagingSocketPath": path, "name": "real-owner"}, "7.k.key")]))
    monkeypatch.setattr(R, "endpoint_kind", lambda platform=None: kind)
    if kind == "socket":
        monkeypatch.setattr(R, "SOCKS", str(tmp_path))
        monkeypatch.setattr(R.stat, "S_ISSOCK", lambda mode: True)


def endpoint(kind, tmp_path):
    if kind == "pipe":
        return PIPE_A
    p = tmp_path / "a.sock"
    p.write_bytes(b"")
    return str(p)


def test_parse_mode_send_to():
    d = digest(PIPE_A)
    assert R.parse_mode(f"send-to 12 {d}") == ("send-to", (12, d))
    assert R.parse_mode("send-to 12") is None
    assert R.parse_mode("send-to x " + d) is None
    assert R.parse_mode("send-to 12 " + d.upper()) is None
    assert R.parse_mode("send-to 12 abc") is None
    assert R.parse_mode(f"send-to 12 {d} extra") is None


@pytest.mark.parametrize("kind", ["pipe", "socket"])
def test_matching_digest_delivers_batched_replies(monkeypatch, tmp_path, capsys, written, kind):
    path = endpoint(kind, tmp_path)
    serve(monkeypatch, kind, path, tmp_path)
    monkeypatch.setattr(R.sys, "stdin", _Stdin(b'{"a":1}\n\n{"b":2}\n'))
    R.send_batch(7, digest(path))
    replies = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [r["ok"] for r in replies] == [True, False, True]
    assert [p for p, _ in written] == [path, path]


@pytest.mark.parametrize("kind", ["pipe", "socket"])
def test_mismatched_digest_exits_before_reading_or_writing(monkeypatch, tmp_path, written, kind):
    path = endpoint(kind, tmp_path)
    serve(monkeypatch, kind, path, tmp_path)
    stdin = _Stdin(b'{"a":1}\n')
    monkeypatch.setattr(R.sys, "stdin", stdin)
    with pytest.raises(SystemExit) as exc:
        R.send_batch(7, digest(PIPE_B if kind == "pipe" else path + "x"))
    assert str(exc.value) == "target mismatch: pid 7 holds real-owner"
    assert stdin.buffer.tell() == 0
    assert written == []


def test_send_batch_without_digest_still_serves_by_pid(monkeypatch, tmp_path, capsys, written):
    serve(monkeypatch, "pipe", PIPE_A, tmp_path)
    monkeypatch.setattr(R.sys, "stdin", _Stdin(b'{"a":1}\n'))
    R.send_batch(7)
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_main_dispatches_send_to_with_the_digest(monkeypatch):
    seen = []
    monkeypatch.setattr(R, "send_batch", lambda pid, dg=None: seen.append((pid, dg)))
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", f"send-to 7 {digest(PIPE_A)}")
    R.main()
    assert seen == [(7, digest(PIPE_A))]
