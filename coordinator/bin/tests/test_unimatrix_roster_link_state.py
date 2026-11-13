"""Pins the roster's link-state record: written once per up/down transition, never per poll."""
import importlib.util
import json
import subprocess
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
    assert writes == ["up", "down", "up"]  # the first record, then one per transition
    assert text.count("LINK-DOWN") == 1 and text.count("LINK-UP") == 2
    rec = record(tmp_path)
    assert rec["state"] == "up" and rec["alias"] == "example-fleet-pro" and rec["since"].endswith("Z")


def test_still_down_record_carries_reason(monkeypatch, tmp_path):
    mod = load()
    run_main(mod, monkeypatch, tmp_path, [False, False, False])
    rec = record(tmp_path)
    assert rec["state"] == "down" and "rc=255" in rec["reason"]


def test_steady_up_writes_the_first_record_once_then_nothing(monkeypatch, tmp_path):
    # A box with no record gets one, so the G-EM watch sees a new box's first state (watch.link_tick).
    mod = load()
    writes = []
    real = mod.write_json_atomic
    monkeypatch.setattr(mod, "write_json_atomic", lambda p, d: (writes.append(d["state"]), real(p, d)))
    text = run_main(mod, monkeypatch, tmp_path, [True, True, True, True])
    assert writes == ["up"] and text.count("LINK-UP") == 1 and record(tmp_path)["state"] == "up"


def test_steady_up_with_a_record_writes_nothing(monkeypatch, tmp_path):
    mod = load()
    d = tmp_path / "home" / "unimatrix" / "link-state"
    d.mkdir(parents=True)
    (d / "example-fleet-pro.json").write_text(json.dumps({"state": "up"}), encoding="utf-8")
    text = run_main(mod, monkeypatch, tmp_path, [True, True, True])
    assert "LINK-" not in text


def test_restart_after_recorded_down_reports_up(monkeypatch, tmp_path):
    mod = load()
    d = tmp_path / "home" / "unimatrix" / "link-state"
    d.mkdir(parents=True)
    (d / "example-fleet-pro.json").write_text(json.dumps({"state": "down"}), encoding="utf-8")
    text = run_main(mod, monkeypatch, tmp_path, [True, True])
    assert text.count("LINK-UP") == 1 and record(tmp_path)["state"] == "up"


TICKS = 6


def drive_default_path(mod, monkeypatch, tmp_path, win, fail_first=False):
    """Run main() for TICKS polls through the real fetch_roster over a fake ssh; returns (argvs, dials, clock)."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    for name in ("sync", "reap_orphaned_mirrors", "reap_unbacked_agy_faces", "ensure_key_loaded"):
        monkeypatch.setattr(mod, name, lambda *a, **k: None)
    monkeypatch.setattr(mod, "inbound_names", lambda: set())
    monkeypatch.setattr(mod, "lan_reachable", lambda alias: True)
    monkeypatch.setattr(mod, "_is_win", lambda platform=None: win)
    clock = {"t": 1000.0}
    monkeypatch.setattr(mod, "_now", lambda: clock["t"])
    argvs, masters, dials = [], set(), []

    def run(argv, **kw):
        argvs.append(argv)
        path = next((a.split("=", 1)[1] for a in argv if a.startswith("ControlPath=")), None)
        if path is None or path not in masters:
            dials.append(argv)
            if path is not None:
                masters.add(path)
        if fail_first and len(argvs) == 1:
            return subprocess.CompletedProcess(argv, 255, b"", b"kex_exchange_identification: closed\n")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(mod.subprocess, "run", run)
    ticks = {"n": 0}

    def sleep(_):
        ticks["n"] += 1
        clock["t"] += mod.POLL_SECONDS
        if ticks["n"] >= TICKS:
            raise Stop

    monkeypatch.setattr(mod.time, "sleep", sleep)
    with pytest.raises(Stop):
        mod.main([str(tmp_path / "roster.log"), "--remote", "example-fleet-pro", "--key", "k"])
    return argvs, dials


def test_posix_poll_multiplexes_every_tick_over_one_connection(monkeypatch, tmp_path):
    mod = load()
    argvs, dials = drive_default_path(mod, monkeypatch, tmp_path, win=False)
    assert len(argvs) == TICKS and len(dials) == 1
    paths = {a for argv in argvs for a in argv if a.startswith("ControlPath=")}
    assert len(paths) == 1
    ctl = Path(paths.pop().split("=", 1)[1])
    assert ctl.is_relative_to(tmp_path / "home") and ctl.parent.is_dir()
    assert all("ControlMaster=auto" in argv for argv in argvs)
    persist = next(a for a in argvs[0] if a.startswith("ControlPersist="))
    assert int(persist.split("=")[1]) > mod.POLL_SECONDS


def test_windows_poll_dials_no_faster_than_the_cap(monkeypatch, tmp_path):
    mod = load()
    argvs, dials = drive_default_path(mod, monkeypatch, tmp_path, win=True)
    assert not any(a.startswith("Control") for argv in argvs for a in argv)
    span = (TICKS - 1) * mod.POLL_SECONDS
    assert 1 <= len(argvs) <= span // mod.WINDOWS_DIAL_MIN_SECONDS + 1
    assert len(argvs) < TICKS


def test_windows_failure_is_not_retried_inside_the_cap_and_replays_its_reason(monkeypatch, tmp_path):
    mod = load()
    argvs, _ = drive_default_path(mod, monkeypatch, tmp_path, win=True, fail_first=True)
    text = (tmp_path / "roster.log").read_text(encoding="utf-8")
    assert len(argvs) >= 2 and "LINK-DOWN" in text and "kex_exchange_identification" in text
    assert text.count("LINK-DOWN") == 1 and text.count("LINK-UP") == 1


def test_record_write_failure_is_logged_and_loop_continues(monkeypatch, tmp_path):
    mod = load()

    def boom(p, d):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "write_json_atomic", boom)
    text = run_main(mod, monkeypatch, tmp_path, [False, False, True])
    assert "link-state write failed: disk full" in text
    assert text.count("LINK-DOWN") == 1 and text.count("LINK-UP") == 1
