"""A keyless inbox mirror never suppresses the far roster row; relay-in frames from an unregistered
sender are refused, and a written line records `written`."""
import importlib.util
import io
import json
import os
from pathlib import Path

BIN = Path(__file__).resolve().parents[1]


def _load(fname, tag):
    spec = importlib.util.spec_from_file_location(tag, BIN / fname)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_keyless_inbox_mirror_is_not_inbound(tmp_path, monkeypatch):
    roster = _load("unimatrix-roster.py", "roster_keyless")
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(tmp_path))
    sock = "/tmp/cc-socks/unimatrix-general-work-98.sock"
    me = os.getpid()
    entry = {"pid": me, "procStart": roster.proc_start(me), "name": "general-work-98",
             "unimatrixMirror": True, "messagingSocketPath": sock}
    (tmp_path / f"{me}.json").write_text(json.dumps(entry))
    assert roster.inbound_names() == set()
    (tmp_path / roster.key_name(me, sock)).write_text("{}")
    assert roster.inbound_names() == {"general-work-98"}


def test_unregistered_sender_refused_and_registered_passes(monkeypatch):
    relay = _load("unimatrix-relay.py", "relay_sender")
    monkeypatch.setattr(relay, "entries", lambda: iter([(1, {"messagingSocketPath": "/s/a.sock"}, "k")]))
    frame = lambda frm: json.dumps({"msgV": 1, "type": "user", "from": frm}).encode()
    assert relay.unregistered_sender(frame("uds:\\\\.\\pipe\\cli")) == "uds:\\\\.\\pipe\\cli"
    monkeypatch.setattr(relay, "entries", lambda: iter([(1, {"messagingSocketPath": "/s/a.sock"}, "k")]))
    assert relay.unregistered_sender(frame("uds:/s/a.sock")) is None


def test_send_batch_refuses_unregistered_sender_and_records_written(tmp_path, monkeypatch):
    relay = _load("unimatrix-relay.py", "relay_batch")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    rows = [(7, {"messagingSocketPath": "/s/a.sock", "sessionId": "x"}, "k")]
    monkeypatch.setattr(relay, "entries", lambda: iter(rows))
    monkeypatch.setattr(relay, "_session_path", lambda *a, **k: "/s/a.sock")
    monkeypatch.setattr(relay, "local_reply_address", lambda line: line)
    sent = []
    monkeypatch.setattr(relay, "_write_line", lambda p, l: sent.append(l))
    bad = json.dumps({"from": "uds:/elsewhere"}).encode() + b"\n"
    good = json.dumps({"from": "uds:/s/a.sock"}).encode() + b"\n"

    class _In:
        buffer = io.BytesIO(bad + good)

    monkeypatch.setattr(relay.sys, "stdin", _In)
    relay.send_batch(7)
    assert len(sent) == 1
    log = [json.loads(x) for x in (tmp_path / "unimatrix" / "deliveries.jsonl").read_text().splitlines()]
    assert log[0]["ok"] is False and "not registered" in log[0]["error"]
    assert log[1]["ok"] is True and log[1]["written"] is True
