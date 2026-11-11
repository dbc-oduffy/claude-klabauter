"""Behavior pins for the spawn-free unimatrix liveness module."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
SRC = BIN / "unimatrix-liveness.py"


def _load():
    spec = importlib.util.spec_from_file_location("unimatrix_liveness", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lv = _load()


def test_module_does_not_import_subprocess():
    src = SRC.read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import subprocess|from subprocess)", src, re.M)
    assert "subprocess" not in lv.__dict__


@pytest.mark.skipif(sys.platform == "win32", reason="ps lstart is POSIX")
def test_proc_start_matches_ps_lstart_for_own_pid():
    want = subprocess.run(["ps", "-o", "lstart=", "-p", str(os.getpid())],
                          capture_output=True, text=True, check=True).stdout.strip()
    assert lv.proc_start(os.getpid()) == want


@pytest.mark.skipif(sys.platform == "win32", reason="ps lstart is POSIX")
def test_proc_start_matches_ps_lstart_for_child_pid():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        want = subprocess.run(["ps", "-o", "lstart=", "-p", str(child.pid)],
                              capture_output=True, text=True, check=True).stdout.strip()
        assert lv.proc_start(child.pid) == want
    finally:
        child.kill()
        child.wait()


def test_lstart_pads_one_digit_day():
    epoch = time.mktime((2026, 11, 4, 1, 19, 29, 0, 0, -1))
    assert lv._lstart(epoch) == "Wed Nov  4 01:19:29 2026"


@pytest.mark.skipif(sys.platform == "win32", reason="posix pid")
def test_proc_start_dead_pid_raises_oserror():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    with pytest.raises(OSError):
        lv.proc_start(p.pid)


def _rec(pid=None):
    pid = pid or os.getpid()
    return {"pid": pid, "procStart": lv.proc_start(pid)}


def test_supervisor_live_true_false_cases():
    assert lv.supervisor_live(_rec())
    assert not lv.supervisor_live({"pid": os.getpid(), "procStart": "Mon Jan  1 00:00:00 2001"})
    assert not lv.supervisor_live({"pid": "x"})
    assert not lv.supervisor_live({})
    assert not lv.supervisor_live({"pid": True, "procStart": "x"})
    assert not lv.pid_alive(0) and not lv.pid_alive(-1)


def test_entry_live_matching_procstart_is_live():
    assert lv.entry_live(_rec())


def test_entry_live_different_procstart_is_not_live():
    assert not lv.entry_live({"pid": os.getpid(), "procStart": "Mon Jan  1 00:00:00 2001"})


def test_entry_live_without_procstart_falls_back_to_pid_alive():
    assert lv.entry_live({"pid": os.getpid()})
    assert lv.entry_live({"pid": os.getpid(), "procStart": ""})
    assert lv.entry_live({"pid": os.getpid(), "procStart": 5})


def test_entry_live_dead_or_malformed_pid_is_not_live():
    assert not lv.entry_live({"pid": 0, "procStart": "x"})
    assert not lv.entry_live({"pid": True})
    assert not lv.entry_live({"pid": "x"})
    assert not lv.entry_live({})
    assert not lv.entry_live(None)


def test_entry_live_proc_start_oserror_is_not_staleness(monkeypatch):
    def boom(pid, platform=None):
        raise OSError("unreadable")
    monkeypatch.setattr(lv, "proc_start", boom)
    assert lv.entry_live({"pid": os.getpid(), "procStart": "anything"})
    monkeypatch.setattr(lv, "pid_alive", lambda pid: False)
    assert not lv.entry_live({"pid": os.getpid(), "procStart": "anything"})


def test_stamp_fresh_accepts_only_a_recent_integer_stamp():
    now = 1_000_000.0
    ms = int(now * 1000)
    assert lv.stamp_fresh({"s": ms - 5000}, "s", 10, now=now) is True
    assert lv.stamp_fresh({"s": ms - 11000}, "s", 10, now=now) is False
    assert lv.stamp_fresh({"s": ms + 1000}, "s", 10, now=now) is False
    for bad in ({}, {"s": "1"}, {"s": True}, {"s": None}, None):
        assert lv.stamp_fresh(bad, "s", 10, now=now) is False


def _agy_home(tmp_path, agy_pid):
    base = tmp_path / "unimatrix"
    (base / "agy-legs").mkdir(parents=True)
    (base / "peers.json").write_text(json.dumps([{"name": "agy-x-on-box", "vendor": "agy", "target": "conv-1"}]))
    if agy_pid is not None:
        (base / "agy-legs" / "conv-1.json").write_text(json.dumps({"pid": 1, "agy_pid": agy_pid}))
    return tmp_path


def test_agy_face_unbacked_when_its_agy_process_is_gone(tmp_path):
    face = {"name": "agy-x-on-box", "vendor": "agy", "unimatrixRouterPid": 7}
    assert lv.agy_face_unbacked(face, _agy_home(tmp_path, 2_000_000_000)) is True


def test_agy_face_backed_while_its_agy_process_lives(tmp_path):
    face = {"name": "agy-x-on-box", "vendor": "agy", "unimatrixRouterPid": 7}
    assert lv.agy_face_unbacked(face, _agy_home(tmp_path, os.getpid())) is False


def test_agy_face_without_a_leg_record_is_unbacked_and_a_non_face_never_is(tmp_path):
    home = _agy_home(tmp_path, None)
    assert lv.agy_face_unbacked({"name": "agy-x-on-box", "vendor": "agy", "unimatrixRouterPid": 7}, home) is True
    assert lv.agy_face_unbacked({"name": "agy-x-on-box", "vendor": "agy"}, home) is False
    assert lv.agy_face_unbacked({"name": "agy-x-on-box", "vendor": "codex", "unimatrixRouterPid": 7}, home) is False


def test_agy_face_with_no_peers_row_is_unbacked_but_an_unreadable_peers_file_is_not(tmp_path):
    home = _agy_home(tmp_path, os.getpid())
    assert lv.agy_face_unbacked({"name": "agy-Temp-bafde89c04-on-machine-a", "vendor": "agy",
                                 "unimatrixRouterPid": 7}, home) is True
    (tmp_path / "unimatrix" / "peers.json").write_text("{not json")
    assert lv.agy_face_unbacked({"name": "agy-x-on-box", "vendor": "agy", "unimatrixRouterPid": 7}, home) is False


def test_codex_holder_live_matching_start_is_live():
    me = _rec()
    assert lv.codex_holder_live({"codexPid": me["pid"], "codexProcStart": me["procStart"]}) is True


def test_codex_holder_live_dead_pid_is_false(monkeypatch):
    monkeypatch.setattr(lv, "pid_alive", lambda pid: False)
    assert lv.codex_holder_live({"codexPid": 4242, "codexProcStart": "x"}) is False


def test_codex_holder_live_differing_start_is_false():
    row = {"codexPid": os.getpid(), "codexProcStart": "Mon Jan  1 00:00:00 2001"}
    assert lv.codex_holder_live(row) is False


def test_codex_holder_live_no_holder_is_none():
    assert lv.codex_holder_live({}) is None
    assert lv.codex_holder_live({"codexPid": True}) is None
    assert lv.codex_holder_live({"codexPid": "7"}) is None
    assert lv.codex_holder_live(None) is None


def test_codex_holder_live_unreadable_start_is_not_staleness(monkeypatch):
    def boom(pid, platform=None):
        raise OSError("unreadable")
    monkeypatch.setattr(lv, "proc_start", boom)
    assert lv.codex_holder_live({"codexPid": os.getpid(), "codexProcStart": "x"}) is True


def test_codex_probe_cli_prints_one_line_per_codex_peer(tmp_path):
    me = _rec()
    locks = tmp_path / "locks"
    locks.mkdir()
    (locks / "thr-live.lock").write_text("")
    (locks / "thr-orphan.lock").write_text("")
    (locks / ".hidden.lock").write_text("")
    peers = tmp_path / "peers.json"
    peers.write_text(json.dumps({"peers": [
        {"name": "codex-a", "vendor": "codex", "target": "thr-live", "cwd": "/w/a",
         "codexPid": me["pid"], "codexProcStart": me["procStart"]},
        {"name": "codex-b", "vendor": "codex", "target": "thr-dead", "cwd": "/w/b",
         "codexPid": me["pid"], "codexProcStart": "Mon Jan  1 00:00:00 2001"},
        {"name": "codex-c", "vendor": "codex", "target": "thr-none"},
        {"name": "claude-x", "vendor": "claude", "target": "t"},
    ]}))
    out = subprocess.run([sys.executable, str(SRC), "codex-probe", "--peers", str(peers),
                          "--locks-dir", str(locks)], capture_output=True, text=True, check=True).stdout
    assert out.splitlines() == [
        f"codex-a thr-live {me['pid']} /w/a live",
        f"codex-b thr-dead {me['pid']} /w/b stale",
        "codex-c thr-none - - stale",
        "codex-thr-orphan thr-orphan - - stale",
    ]


def test_codex_probe_cli_rejects_unknown_command():
    r = subprocess.run([sys.executable, str(SRC), "bogus"], capture_output=True, text=True)
    assert r.returncode == 2


def _setup(tmp_path, aliases, live=True, age=0):
    sdir = tmp_path / "unimatrix"
    sdir.mkdir()
    cache = sdir / "autolink-boxes.json"
    cache.write_text(json.dumps({"aliases": aliases}))
    if age:
        t = time.time() - age
        os.utime(cache, (t, t))
    for a in aliases:
        rec = {"bridge": _rec(), "inbox": _rec()} if live else {"bridge": _rec(), "inbox": {"pid": 0}}
        (sdir / f"autolink-{lv._slug(a)}.json").write_text(json.dumps(rec))
    return sdir


def test_healthy_true_when_all_live(tmp_path):
    assert lv.healthy(_setup(tmp_path, ["a@h", "b"]))


def test_healthy_true_for_fresh_empty_cache(tmp_path):
    assert lv.healthy(_setup(tmp_path, []))


def test_healthy_false_when_one_role_dead(tmp_path):
    assert not lv.healthy(_setup(tmp_path, ["a"], live=False))


def test_healthy_false_when_record_missing(tmp_path):
    sdir = _setup(tmp_path, ["a"])
    (sdir / "autolink-a.json").unlink()
    assert not lv.healthy(sdir)


def test_healthy_false_without_cache(tmp_path):
    (tmp_path / "unimatrix").mkdir()
    assert not lv.healthy(tmp_path / "unimatrix")
    assert not lv.healthy(tmp_path / "absent")


def test_healthy_false_for_stale_cache_over_24h(tmp_path):
    assert not lv.healthy(_setup(tmp_path, ["a"], age=24 * 3600 + 5))


def test_healthy_true_just_under_24h(tmp_path):
    assert lv.healthy(_setup(tmp_path, ["a"], age=24 * 3600 - 60))


def test_healthy_honors_now(tmp_path):
    sdir = _setup(tmp_path, ["a"])
    assert not lv.healthy(sdir, now=time.time() + 25 * 3600)


def test_healthy_false_for_malformed_cache(tmp_path):
    sdir = _setup(tmp_path, [])
    (sdir / "autolink-boxes.json").write_text(json.dumps({"aliases": "a"}))
    assert not lv.healthy(sdir)


def test_backoff_doubles_caps_and_clears(tmp_path):
    assert lv.backoff_until(tmp_path, "a") == 0.0
    expect = [60, 120, 240, 480, 960, 1920, 3600, 3600]
    for n, delay in enumerate(expect):
        lv.note_probe(tmp_path, "a", False, now=1000.0)
        assert lv.backoff_until(tmp_path, "a") == 1000.0 + delay, n
    assert json.loads((tmp_path / "autolink-a.backoff.json").read_text())["failures"] == len(expect)
    lv.note_probe(tmp_path, "a", True)
    assert lv.backoff_until(tmp_path, "a") == 0.0
    assert not (tmp_path / "autolink-a.backoff.json").exists()
    lv.note_probe(tmp_path, "a", True)


def test_backoff_is_per_alias_and_slugged(tmp_path):
    lv.note_probe(tmp_path, "u@h", False, now=0.0)
    assert (tmp_path / "autolink-u_at_h.backoff.json").exists()
    assert lv.backoff_until(tmp_path, "other") == 0.0


def test_state_dir_derives_from_settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    assert lv.state_dir() == tmp_path / "unimatrix"


def _logs_db(home, name, rows):
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(home / name)) as conn:
        conn.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
                     "ts_nanos INTEGER NOT NULL, thread_id TEXT, process_uuid TEXT)")
        conn.executemany("INSERT INTO logs (ts, ts_nanos, thread_id, process_uuid) VALUES (?,?,?,?)", rows)
        conn.commit()


def test_codex_thread_holder_takes_newest_process_across_dbs(tmp_path):
    me = os.getpid()
    _logs_db(tmp_path, "logs_1.sqlite", [(10, 0, "t1", "pid:999999:old"), (5, 0, "t2", "pid:7:x")])
    _logs_db(tmp_path, "logs_2.sqlite", [(20, 0, "t1", f"pid:{me}:new"), (30, 0, "t1", None)])
    got = lv.codex_thread_holder(tmp_path, "t1")
    assert got["codexPid"] == me
    assert got["codexProcStart"] == lv.proc_start(me)
    assert lv.codex_holder_live(got) is True


def test_codex_thread_holder_unknown_thread_or_no_db_is_empty(tmp_path):
    assert lv.codex_thread_holder(tmp_path, "t1") == {}
    _logs_db(tmp_path, "logs_1.sqlite", [(10, 0, "other", "pid:5:x"), (11, 0, "t1", "garbage")])
    assert lv.codex_thread_holder(tmp_path, "t1") == {}
    assert lv.codex_thread_holder(tmp_path, "missing") == {}


def test_codex_thread_holder_dead_pid_reads_dead(tmp_path):
    _logs_db(tmp_path, "logs_1.sqlite", [(10, 0, "t1", "pid:2147480000:x")])
    got = lv.codex_thread_holder(tmp_path, "t1")
    assert got == {"codexPid": 2147480000}
    assert lv.codex_holder_live(got) is False
