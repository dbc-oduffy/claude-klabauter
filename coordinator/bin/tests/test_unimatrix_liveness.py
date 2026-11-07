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
