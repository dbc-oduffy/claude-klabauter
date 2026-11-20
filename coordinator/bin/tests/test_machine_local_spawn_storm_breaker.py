"""Pins the machine-local re-entry guard: a nested or saturated invocation is refused before any work.

In-process tests drive the breaker directly; subprocess tests drive the in-flight slot cap and
a normal call across a real process boundary.
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process]

SCRIPT = Path(__file__).resolve().parents[2] / "templates" / "bin" / "_machine_local.py"


@pytest.fixture
def ml(monkeypatch):
    spec = importlib.util.spec_from_file_location("_machine_local_storm_breaker_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv(mod._DEPTH_ENV, "0")  # registers restore: the breaker sets it in os.environ
    return mod


@pytest.fixture
def slotted(ml, tmp_path, monkeypatch):
    monkeypatch.setenv(ml._SLOT_DIR_ENV, str(tmp_path / "slots"))
    return ml


def test_nested_invocation_is_refused(ml, monkeypatch, capsys):
    monkeypatch.setenv(ml._DEPTH_ENV, "1")
    assert ml._spawn_storm_breaker() == ml.EXIT_OPERATIONAL
    err = capsys.readouterr()
    assert "refused nested invocation" in err.err
    assert err.out == ""


def test_top_level_invocation_proceeds_and_marks_descendants(slotted, monkeypatch):
    monkeypatch.setattr(slotted.os, "getloadavg", lambda: (0.0, 0.0, 0.0), raising=False)
    assert slotted._spawn_storm_breaker() is None
    assert os.environ[slotted._DEPTH_ENV] == "1", "children inherit the depth var and refuse re-entry"
    os.close(slotted._held_slot)


def test_saturated_box_is_refused(slotted, monkeypatch, capsys):
    monkeypatch.setattr(slotted.os, "cpu_count", lambda: 2)
    monkeypatch.setattr(slotted.os, "getloadavg", lambda: (17.0, 0.0, 0.0), raising=False)
    assert slotted._spawn_storm_breaker() == slotted.EXIT_OPERATIONAL
    assert "refused under load" in capsys.readouterr().err
    os.close(slotted._held_slot)


def test_load_at_limit_proceeds(slotted, monkeypatch):
    monkeypatch.setattr(slotted.os, "cpu_count", lambda: 2)
    monkeypatch.setattr(slotted.os, "getloadavg", lambda: (16.0, 0.0, 0.0), raising=False)
    assert slotted._spawn_storm_breaker() is None
    os.close(slotted._held_slot)


def test_no_loadavg_proceeds(slotted, monkeypatch):
    def _absent():
        raise OSError("no load average")
    monkeypatch.setattr(slotted.os, "getloadavg", _absent, raising=False)
    assert slotted._spawn_storm_breaker() is None
    os.close(slotted._held_slot)


def test_default_cap_is_sixteen(ml, monkeypatch):
    monkeypatch.delenv(ml._MAX_INFLIGHT_ENV, raising=False)
    assert ml._max_inflight() == 16


def _run(env_extra, tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "MACHINE_LOCAL_DEPTH"}
    env.update({"MACHINE_LOCAL_REGISTRY_DIR": str(tmp_path), **env_extra})
    return subprocess.run([sys.executable, str(SCRIPT), "get", "repos.nope", "--default", "x"],
                          capture_output=True, text=True, env=env, timeout=30,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def test_top_level_invocation_runs(tmp_path):
    r = _run({"MACHINE_LOCAL_SLOT_DIR": str(tmp_path / "slots")}, tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "x"


def test_nested_subprocess_exits_2(tmp_path):
    r = _run({"MACHINE_LOCAL_SLOT_DIR": str(tmp_path / "slots"), "MACHINE_LOCAL_DEPTH": "1"}, tmp_path)
    assert r.returncode == 2 and "refused nested invocation" in r.stderr
    assert r.stdout == ""


def _hold_slot(path):
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_full_inflight_slots_refuse_at_once_and_a_freed_slot_admits(tmp_path):
    slots = tmp_path / "slots"
    slots.mkdir()
    extra = {"MACHINE_LOCAL_SLOT_DIR": str(slots), "MACHINE_LOCAL_MAX_INFLIGHT": "1"}
    fd = _hold_slot(slots / "slot-0.lock")
    try:
        r = _run(extra, tmp_path)
        assert r.returncode == 2 and "in flight" in r.stderr
        assert r.stdout == ""
    finally:
        os.close(fd)  # closing the descriptor releases the lock, as process death does
    r = _run(extra, tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "x"
