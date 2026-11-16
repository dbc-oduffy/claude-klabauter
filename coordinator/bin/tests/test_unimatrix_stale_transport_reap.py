"""A reply-inbox transport superseded by a newer one of the same near box is reaped with its own socket."""
import importlib.util
import json
from pathlib import Path

BIN = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("stale_transport_relay", BIN / "unimatrix-relay.py")
relay = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(relay)

CLIENT = "10.0.0.1"


def _transport(name, client=CLIENT, **extra):
    return {"name": name, "unimatrixMirror": True, "unimatrixReplyInbox": True,
            "unimatrixClient": client, **extra}


def _patch_entries(monkeypatch, rows):
    monkeypatch.setattr(relay, "entries", lambda: iter(rows))


def test_only_other_named_transports_of_the_same_box_are_superseded(monkeypatch):
    rows = [(1, _transport("t-old"), "1.k"),
            (2, _transport("t-new"), "2.k"),
            (3, _transport("t-other-box", client="10.0.0.2"), "3.k"),
            (4, {"name": "plain", "unimatrixMirror": True, "unimatrixClient": CLIENT}, "4.k"),
            (5, _transport("t-self"), "5.k")]
    _patch_entries(monkeypatch, rows)
    got = relay.superseded_transports("t-new", CLIENT, own_pid=5)
    assert [p for p, _, _ in got] == [1]
    assert relay.superseded_transports("t-new", "", own_pid=5) == []


def test_reap_kills_and_unlinks_only_the_inode_it_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "SESS", str(tmp_path))
    sock = tmp_path / "old.sock"
    sock.write_text("")
    ours = tmp_path / "7.json"
    key = tmp_path / "7.k.key"
    ours.write_text("{}")
    key.write_text("k")
    d = _transport("t-old", messagingSocketPath=str(sock), unimatrixSocketIno=sock.stat().st_ino)
    killed = []
    monkeypatch.setattr(relay.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    relay.reap_transports([(7, d, key.name)])
    assert killed == [(7, relay.signal.SIGTERM)]
    assert not sock.exists() and not ours.exists() and not key.exists()


def test_reap_leaves_a_socket_a_successor_rebound(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "SESS", str(tmp_path))
    sock = tmp_path / "shared.sock"
    sock.write_text("")
    recorded = sock.stat().st_ino
    sock.unlink()
    sock.write_text("")  # a new inode now holds the path
    if sock.stat().st_ino == recorded:  # filesystem recycled the inode number; the pin cannot be exercised
        return
    (tmp_path / "7.json").write_text("{}")
    (tmp_path / "7.k.key").write_text("k")
    d = _transport("t-old", messagingSocketPath=str(sock), unimatrixSocketIno=recorded)
    monkeypatch.setattr(relay.os, "kill", lambda pid, sig: None)
    relay.reap_transports([(7, d, "7.k.key")])
    assert sock.exists() and not (tmp_path / "7.json").exists()


def test_entry_without_a_recorded_inode_keeps_its_socket(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "SESS", str(tmp_path))
    sock = tmp_path / "legacy.sock"
    sock.write_text("")
    (tmp_path / "7.json").write_text("{}")
    (tmp_path / "7.k.key").write_text("k")
    monkeypatch.setattr(relay.os, "kill", lambda pid, sig: None)
    relay.reap_transports([(7, _transport("t", messagingSocketPath=str(sock)), "7.k.key")])
    assert sock.exists()


def test_roster_mirrors_lists_plain_mirrors_and_default_roster_still_hides_them(monkeypatch, capsys):
    mirror = {"pid": 4, "name": "far", "unimatrixMirror": True, "messagingSocketPath": "/m.sock"}
    monkeypatch.setattr(relay, "entries", lambda: iter([(4, mirror, "4.k")]))
    monkeypatch.setattr(relay, "engine_fields", lambda: {})
    monkeypatch.setattr(relay, "load_roster", lambda: object())
    relay.roster()
    assert capsys.readouterr().out == ""
    relay.roster(mirrors=True)
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["entry"]["name"] for r in rows] == ["far"] and "key" not in rows[0]
    assert relay.parse_mode("roster --mirrors") == ("roster", "mirrors")
    assert relay.parse_mode("roster --other") is None
