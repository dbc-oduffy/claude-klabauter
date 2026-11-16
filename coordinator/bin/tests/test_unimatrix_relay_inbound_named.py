"""The receiving box's relay delivers a named cross-box send to exactly the addressed live session,
refuses an unknown or mismatched target, records both outcomes, and names the session it reached.

Hermetic: tmp_path settings home, `entries` and `_write_line` monkeypatched, no socket opened.
"""
import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest

RELAY = Path(__file__).resolve().parents[1] / "unimatrix-relay.py"
PIPE_66, PIPE_A1 = "\\\\.\\pipe\\LOCAL\\cc-msg-66", "\\\\.\\pipe\\LOCAL\\cc-msg-a1"
SID_66, SID_A1 = "sid-example-game-repo-66", "sid-claude-klabauter-a1"


def _load():
    spec = importlib.util.spec_from_file_location("unimatrix_relay_inbound_named", RELAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = _load()


class _Stdin:
    def __init__(self, data):
        self.buffer = io.BytesIO(data)


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _rows():
    return [
        (11, {"name": "example-game-workbench-repo-66", "sessionId": SID_66, "messagingSocketPath": PIPE_66}, "11.key"),
        (22, {"name": "claude-klabauter-a1", "sessionId": SID_A1, "messagingSocketPath": PIPE_A1}, "22.key"),
    ]


@pytest.fixture
def box(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.setattr(R, "entries", lambda: iter(_rows()))
    monkeypatch.setattr(R, "endpoint_kind", lambda platform=None: "pipe")
    monkeypatch.setattr(R, "_exported_name", lambda d: d.get("name"))
    sent = []
    monkeypatch.setattr(R, "_write_line", lambda path, line: sent.append(path))
    monkeypatch.setattr(R, "record_receipt", lambda sid, line, now=None: "d" * 64)
    monkeypatch.setattr(R, "_router_resolve", lambda name: None)
    return sent, tmp_path / "unimatrix" / "deliveries.jsonl"


def _send(monkeypatch, pid, endpoint, name, sid):
    monkeypatch.setattr(R.sys, "stdin", _Stdin(b'{"msg":1}\n'))
    R.send_batch(pid, _digest(endpoint), _digest(name), sid)


def _records(path):
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]


def test_named_send_reaches_the_named_session_and_the_receipt_names_it(box, monkeypatch, capsys):
    sent, deliveries = box
    _send(monkeypatch, 11, PIPE_66, "example-game-workbench-repo-66", SID_66)
    reply = json.loads(capsys.readouterr().out)
    assert sent == [PIPE_66]
    assert reply["ok"] is True and reply["reached"] == "example-game-workbench-repo-66"
    assert reply["reached_session"] == SID_66 and reply["reached_pid"] == 11
    [row] = _records(deliveries)
    assert row["ok"] is True and row["name"] == "example-game-workbench-repo-66" and row["session"] == SID_66


def test_send_naming_example_game_repo_66_never_lands_on_claude_klabauter_a1_when_the_pid_is_the_other_session(box, monkeypatch):
    sent, deliveries = box
    with pytest.raises(SystemExit) as refused:
        _send(monkeypatch, 22, PIPE_66, "example-game-workbench-repo-66", SID_66)
    assert str(refused.value).startswith("target mismatch") and sent == []
    [row] = _records(deliveries)
    assert row["ok"] is False and row["pid"] == 22 and row["session"] == SID_66


def test_unknown_target_is_refused_and_recorded_not_delivered_elsewhere(box, monkeypatch):
    sent, deliveries = box
    with pytest.raises(SystemExit) as refused:
        _send(monkeypatch, 99, PIPE_66, "example-game-workbench-repo-66", SID_66)
    assert str(refused.value) == "no live session with pid 99" and sent == []
    [row] = _records(deliveries)
    assert row["ok"] is False and row["error"] == "no live session with pid 99"


def test_mux_pid_resolves_to_the_entry_the_address_names(box, monkeypatch, capsys):
    sent, _ = box
    mux = [(5, {"name": "alpha", "sessionId": "sid-alpha", "messagingSocketPath": "\\\\.\\pipe\\LOCAL\\cc-alpha"}, "5.k"),
           (5, {"name": "beta", "sessionId": "sid-beta", "messagingSocketPath": "\\\\.\\pipe\\LOCAL\\cc-beta"}, "5.k")]
    monkeypatch.setattr(R, "entries", lambda: iter(mux))
    _send(monkeypatch, 5, "\\\\.\\pipe\\LOCAL\\cc-alpha", "alpha", "sid-alpha")
    assert sent == ["\\\\.\\pipe\\LOCAL\\cc-alpha"]
    assert json.loads(capsys.readouterr().out)["reached"] == "alpha"


def test_mux_pid_with_no_unique_match_is_refused(box, monkeypatch):
    sent, _ = box
    mux = [(5, {"name": "alpha", "sessionId": "s1", "messagingSocketPath": "\\\\.\\pipe\\LOCAL\\cc-alpha"}, "5.k"),
           (5, {"name": "beta", "sessionId": "s2", "messagingSocketPath": "\\\\.\\pipe\\LOCAL\\cc-beta"}, "5.k")]
    monkeypatch.setattr(R, "entries", lambda: iter(mux))
    with pytest.raises(SystemExit) as refused:
        _send(monkeypatch, 5, "\\\\.\\pipe\\LOCAL\\cc-gamma", "gamma", "s3")
    assert str(refused.value).startswith("target mismatch") and sent == []


def test_router_refusal_stops_the_delivery_and_records_it(box, monkeypatch):
    sent, deliveries = box
    monkeypatch.setattr(R, "_router_resolve", lambda name: {"ok": False, "error": "no unique live session"})
    with pytest.raises(SystemExit) as refused:
        _send(monkeypatch, 11, PIPE_66, "example-game-workbench-repo-66", SID_66)
    assert str(refused.value).startswith("router refused target") and sent == []
    [row] = _records(deliveries)
    assert row["ok"] is False and "router refused" in row["error"]


def test_router_resolving_to_another_endpoint_is_refused(box, monkeypatch):
    sent, _ = box
    monkeypatch.setattr(R, "_router_resolve", lambda name: {"ok": True, "endpoint": PIPE_A1})
    with pytest.raises(SystemExit) as refused:
        _send(monkeypatch, 11, PIPE_66, "example-game-workbench-repo-66", SID_66)
    assert str(refused.value).startswith("target mismatch") and sent == []


def test_router_agreeing_delivers_and_an_absent_router_does_not_block(box, monkeypatch, capsys):
    sent, _ = box
    monkeypatch.setattr(R, "_router_resolve", lambda name: {"ok": True, "endpoint": PIPE_66})
    _send(monkeypatch, 11, PIPE_66, "example-game-workbench-repo-66", SID_66)
    assert sent == [PIPE_66] and json.loads(capsys.readouterr().out)["ok"] is True
