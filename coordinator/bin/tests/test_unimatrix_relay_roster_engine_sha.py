"""Roster lines carry the box's engine identity; the near-box mirror keeps it."""
import importlib.util
import json
from pathlib import Path

BIN = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, BIN / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_box(tmp_path, monkeypatch, stamp="abc123", freshness=None):
    root = tmp_path / "engine"
    (root / "coordinator_core").mkdir(parents=True)
    if stamp is not None:
        (root / "coordinator_core" / "_engine_stamp").write_text(stamp + "\n")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / ".git" / "refs" / "heads").mkdir(parents=True)
    (root / ".git" / "refs" / "heads" / "main").write_text("f" * 40 + "\n")
    home = tmp_path / "home"
    if freshness is not None:
        (home / "coordinator").mkdir(parents=True)
        (home / "coordinator" / "klabauter-freshness.json").write_text(json.dumps(freshness))
    monkeypatch.setenv("COORDINATOR_ENGINE_SOURCE_ROOT", str(root))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))


def run_roster(relay, tmp_path, monkeypatch, capsys):
    sess = tmp_path / "sess"
    sess.mkdir()
    (sess / "k").write_text("body")
    monkeypatch.setattr(relay, "SESS", str(sess))
    monkeypatch.setattr(relay, "entries", lambda: [(1, {"pid": 1, "messagingSocketPath": "/s"}, "k")])
    relay.roster()
    return [json.loads(ln) for ln in capsys.readouterr().out.splitlines()]


def test_every_line_has_engine_sha_and_checkout_sha(tmp_path, monkeypatch, capsys):
    make_box(tmp_path, monkeypatch)
    rows = run_roster(load("relay_a", "unimatrix-relay.py"), tmp_path, monkeypatch, capsys)
    assert rows[0]["engine_sha"] == "abc123"
    assert rows[0]["engine_checkout_sha"] == "f" * 40
    assert "engine_freshness" not in rows[0]


def test_freshness_present_only_when_record_exists(tmp_path, monkeypatch, capsys):
    rec = {"hostname": "h", "local_sha": "a", "origin_sha": "b", "state": "dirty"}
    make_box(tmp_path, monkeypatch, freshness=rec)
    rows = run_roster(load("relay_b", "unimatrix-relay.py"), tmp_path, monkeypatch, capsys)
    assert rows[0]["engine_freshness"] == rec


def test_unstamped_box_still_reports_key(tmp_path, monkeypatch, capsys):
    make_box(tmp_path, monkeypatch, stamp=None)
    rows = run_roster(load("relay_c", "unimatrix-relay.py"), tmp_path, monkeypatch, capsys)
    assert "engine_sha" in rows[0] and rows[0]["engine_sha"] is None


def test_mirror_helper_passes_far_fields_and_nothing_else():
    roster = load("roster_a", "unimatrix-roster.py")
    row = {"entry": {"pid": 1}, "key": "k", "engine_sha": "s", "engine_checkout_sha": "c",
           "engine_freshness": {"state": "dirty"}}
    assert roster.engine_fields(row) == {
        "engine_sha": "s", "engine_checkout_sha": "c", "engine_freshness": {"state": "dirty"}}
    assert roster.engine_fields({"entry": {}, "key": "k"}) == {}
