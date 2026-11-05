"""Pins the roster's link-state record: written once per up/down transition, never per poll."""
import importlib.util
import json
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("unimatrix_roster_ls", BIN / "unimatrix-roster.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Stop(Exception):
    pass


def run_main(mod, monkeypatch, tmp_path, outcomes):
    """Drive main() through `outcomes` (True = roster answers, False = fails), then stop."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(mod, "inbound_names", lambda: set())
    monkeypatch.setattr(mod, "sync", lambda *a, **k: None)
    seq = iter(outcomes)

    def fetch(*a, **k):
        if not next(seq):
            raise OSError("roster rc=255: down")
        return []

    monkeypatch.setattr(mod, "fetch_roster", fetch)
    polls = {"n": 0}

    def sleep(_):
        polls["n"] += 1
        if polls["n"] >= len(outcomes):
            raise Stop

    monkeypatch.setattr(mod.time, "sleep", sleep)
    log = tmp_path / "roster.log"
    with pytest.raises(Stop):
        mod.main([str(log), "--remote", "example-fleet-pro", "--key", "k"])
    return log.read_text(encoding="utf-8")


def record(tmp_path):
    return json.loads((tmp_path / "home" / "unimatrix" / "link-state" / "example-fleet-pro.json").read_text(encoding="utf-8"))


def test_down_then_up_writes_once_each(monkeypatch, tmp_path):
    mod = load()
    writes = []
    real = mod.write_json_atomic
    monkeypatch.setattr(mod, "write_json_atomic", lambda p, d: (writes.append(d["state"]), real(p, d)))
    text = run_main(mod, monkeypatch, tmp_path, [True, False, False, False, False, True, True])
    assert writes == ["down", "up"]
    assert text.count("LINK-DOWN") == 1 and text.count("LINK-UP") == 1
    rec = record(tmp_path)
    assert rec["state"] == "up" and rec["alias"] == "example-fleet-pro" and rec["since"].endswith("Z")


def test_still_down_record_carries_reason(monkeypatch, tmp_path):
    mod = load()
    run_main(mod, monkeypatch, tmp_path, [False, False, False])
    rec = record(tmp_path)
    assert rec["state"] == "down" and "rc=255" in rec["reason"]


def test_steady_up_writes_nothing(monkeypatch, tmp_path):
    mod = load()
    text = run_main(mod, monkeypatch, tmp_path, [True, True, True])
    assert "LINK-" not in text
    assert not (tmp_path / "home" / "unimatrix" / "link-state").exists()


def test_restart_after_recorded_down_reports_up(monkeypatch, tmp_path):
    mod = load()
    d = tmp_path / "home" / "unimatrix" / "link-state"
    d.mkdir(parents=True)
    (d / "example-fleet-pro.json").write_text(json.dumps({"state": "down"}), encoding="utf-8")
    text = run_main(mod, monkeypatch, tmp_path, [True, True])
    assert text.count("LINK-UP") == 1 and record(tmp_path)["state"] == "up"


def test_record_write_failure_is_logged_and_loop_continues(monkeypatch, tmp_path):
    mod = load()

    def boom(p, d):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "write_json_atomic", boom)
    text = run_main(mod, monkeypatch, tmp_path, [False, False, True])
    assert "link-state write failed: disk full" in text
    assert text.count("LINK-DOWN") == 1 and text.count("LINK-UP") == 1
