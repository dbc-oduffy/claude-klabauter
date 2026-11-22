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


def run_main(mod, monkeypatch, tmp_path, outcomes, down_msg="roster rc=255: down", fetch=None):
    """Drive main() through `outcomes` (True = the connect probe answers, False = it fails with `down_msg`), then stop."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(mod, "inbound_names", lambda: set())
    monkeypatch.setattr(mod, "sync", lambda *a, **k: None)
    seq = iter(outcomes)
    monkeypatch.setattr(mod, "link_probe", lambda *a, **k: (True, "") if next(seq) else (False, down_msg))
    monkeypatch.setattr(mod, "fetch_roster", fetch or (lambda *a, **k: []))
    polls = {"n": 0}

    def sleep(_):
        polls["n"] += 1
        if polls["n"] >= len(outcomes):
            raise Stop

    monkeypatch.setattr(mod.time, "sleep", sleep)
    # With an own start the loop sleeps in stop-poll slices; one sleep per poll keeps the tick count.
    monkeypatch.setattr(mod, "wait_or_stop", lambda seconds, own_start, sleep=sleep: sleep(seconds) or False)
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
    monkeypatch.setattr(mod, "write_json_atomic", lambda p, d: ("state" in d and writes.append(d["state"]), real(p, d)))
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


def test_steady_up_writes_the_first_record_once_then_nothing(monkeypatch, tmp_path):
    # A box with no record gets one, so the G-EM watch sees a new box's first state (watch.link_tick).
    mod = load()
    writes = []
    real = mod.write_json_atomic
    monkeypatch.setattr(mod, "write_json_atomic", lambda p, d: ("state" in d and writes.append(d["state"]), real(p, d)))
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
    mod._box_mux()._FAR_PLATFORM.clear()
    if not win:
        far = tmp_path / "home" / "unimatrix" / "control-far"
        far.mkdir(parents=True)
        (far / "example-fleet-pro.json").write_text(json.dumps({"platform": "linux"}))
    for name in ("sync", "reap_orphaned_mirrors", "reap_unbacked_agy_faces", "ensure_key_loaded"):
        monkeypatch.setattr(mod, name, lambda *a, **k: None)
    monkeypatch.setattr(mod, "inbound_names", lambda: set())
    monkeypatch.setattr(mod, "lan_reachable", lambda alias: True)
    monkeypatch.setattr(mod, "link_probe", lambda *a, **k: (True, ""))
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
        if fail_first and len(argvs) <= mod.RESET_TRIES:
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
    # With an own start the loop sleeps in stop-poll slices; one sleep per poll keeps the tick count.
    monkeypatch.setattr(mod, "wait_or_stop", lambda seconds, own_start, sleep=sleep: sleep(seconds) or False)
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


def test_windows_failure_is_not_retried_inside_the_cap_and_marks_the_roster_stale_once(monkeypatch, tmp_path):
    mod = load()
    argvs, _ = drive_default_path(mod, monkeypatch, tmp_path, win=True, fail_first=True)
    text = (tmp_path / "roster.log").read_text(encoding="utf-8")
    assert len(argvs) >= 2 and "kex_exchange_identification" in text
    assert text.count("ROSTER-STALE") == 1 and "LINK-DOWN" not in text and text.count("LINK-UP") == 1


def test_record_write_failure_is_logged_and_loop_continues(monkeypatch, tmp_path):
    mod = load()

    def boom(p, d):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "write_json_atomic", boom)
    text = run_main(mod, monkeypatch, tmp_path, [False, False, True, True])
    assert "link-state write failed: disk full" in text
    assert text.count("LINK-DOWN") == 1 and text.count("LINK-UP") == 1


@pytest.mark.parametrize("exc", [OSError("roster rc=255: down"), subprocess.TimeoutExpired("ssh", 20)])
def test_roster_failure_never_writes_link_down(monkeypatch, tmp_path, exc):
    mod = load()

    def fetch(*a, **k):
        raise exc

    text = run_main(mod, monkeypatch, tmp_path, [True] * 6, fetch=fetch)
    assert "LINK-DOWN" not in text and text.count("ROSTER-STALE") == 1
    assert record(tmp_path)["state"] == "up"


def test_a_probe_failing_twice_writes_down_whatever_the_roster_does(monkeypatch, tmp_path):
    mod = load()
    text = run_main(mod, monkeypatch, tmp_path, [False, False], down_msg="connect refused", fetch=lambda *a, **k: [])
    assert text.count("LINK-DOWN") == 1 and "ROSTER-STALE" not in text
    rec = record(tmp_path)
    assert rec["state"] == "down" and "connect refused" in rec["reason"]


def test_a_stale_roster_is_not_refetched_inside_its_backoff_while_the_probe_keeps_running(monkeypatch, tmp_path):
    mod = load()
    fetches = []

    def fetch(*a, **k):
        fetches.append(1)
        raise OSError("roster rc=255: slow")

    run_main(mod, monkeypatch, tmp_path, [True] * 4, fetch=fetch)
    assert len(fetches) == 1


def test_link_probe_resolves_once_and_a_steady_tick_spawns_no_process(monkeypatch):
    mod = load()
    spawned, dialled = [], []
    monkeypatch.setattr(mod, "ssh_fields", lambda alias: spawned.append(alias) or {"hostname": "10.0.0.7", "port": "22"})
    connect = lambda ep, timeout: dialled.append(ep) or socket_stub
    socket_stub = type("S", (), {"close": lambda self: None})()
    for _ in range(3):
        assert mod.link_probe("example-fleet-pro", connect=connect) == (True, "")
    assert spawned == ["example-fleet-pro"] and dialled == [("10.0.0.7", 22)] * 3


def test_link_probe_re_resolves_after_a_failed_connect_and_follows_a_moved_endpoint(monkeypatch):
    mod = load()
    hosts = iter(["10.0.0.7", "10.0.0.8"])
    monkeypatch.setattr(mod, "ssh_fields", lambda alias: {"hostname": next(hosts), "port": "22"})

    def connect(ep, timeout):
        if ep[0] == "10.0.0.7":
            raise OSError("timed out")
        return type("S", (), {"close": lambda self: None})()

    assert mod.link_probe("example-fleet-pro", connect=connect) == (True, "")


def test_link_probe_down_names_the_endpoint_and_tries_the_tunnel_alias(monkeypatch):
    mod = load()
    monkeypatch.setattr(mod, "ssh_fields", lambda alias: {"hostname": alias + ".example", "port": "22"})
    dialled = []

    def connect(ep, timeout):
        dialled.append(ep[0])
        raise OSError("refused")

    ok, why = mod.link_probe("lan", "tun", connect=connect)
    assert not ok and "lan.example:22" in why and "tun.example:22" in why and "lan.example" in dialled and "tun.example" in dialled


def test_a_roster_slower_than_the_old_budget_cannot_mark_the_link_down(monkeypatch, tmp_path):
    # The roster ssh hung to its 20s budget (TimeoutExpired inside the channel, returned as rc 255) on every
    # poll while the far sshd kept accepting connects: only the roster goes stale.
    mod = load()
    monkeypatch.setattr(mod, "_is_win", lambda platform=None: False)
    monkeypatch.setattr(mod, "ensure_key_loaded", lambda *a, **k: None)
    monkeypatch.setattr(mod, "pick_remote", lambda lan, tunnel=None, reachable=None, key=None: lan)
    monkeypatch.setattr(mod._box_mux(), "verified_host", lambda host, *a, **k: host)

    def hung(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))

    monkeypatch.setattr(mod.subprocess, "run", hung)
    text = run_main(mod, monkeypatch, tmp_path, [True] * 5, fetch=mod.fetch_roster)
    assert "ROSTER-STALE" in text and "TimeoutExpired" in text and "LINK-DOWN" not in text
    assert record(tmp_path)["state"] == "up"


def test_one_failed_poll_between_good_ones_writes_and_logs_nothing(monkeypatch, tmp_path):
    mod = load()
    d = tmp_path / "home" / "unimatrix" / "link-state"
    d.mkdir(parents=True)
    (d / "example-fleet-pro.json").write_text(json.dumps({"state": "up"}), encoding="utf-8")
    text = run_main(mod, monkeypatch, tmp_path, [True, False, True, True])
    assert "LINK-" not in text
    assert record(tmp_path) == {"state": "up"}


def test_two_consecutive_failures_flip_down_and_one_good_poll_does_not_flip_back(monkeypatch, tmp_path):
    mod = load()
    text = run_main(mod, monkeypatch, tmp_path, [False, False, True, False, False])
    assert text.count("LINK-DOWN") == 1 and "LINK-UP" not in text
    assert record(tmp_path)["state"] == "down"


def test_non_consecutive_failures_never_accumulate(monkeypatch, tmp_path):
    mod = load()
    text = run_main(mod, monkeypatch, tmp_path, [False, True, False, True, False, True])
    assert "LINK-" not in text
