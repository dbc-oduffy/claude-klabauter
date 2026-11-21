"""Pins the machine-local re-entry guard: a nested or saturated invocation is refused before any work.

In-process tests drive the breaker directly; subprocess tests drive the in-flight slot cap and
a normal call across a real process boundary.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SCRIPT = Path(__file__).resolve().parents[2] / "templates" / "bin" / "_machine_local.py"


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
