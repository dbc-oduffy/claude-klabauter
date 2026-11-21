"""Tests for coordinator_core.machine_local_spawn: bounded spawn, in-process dump, cache, single-flight."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core import locked_write
from coordinator_core import machine_local_spawn as mls

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REPO_ROOT = Path(__file__).resolve().parents[2]
KERNEL = REPO_ROOT / "coordinator" / "templates" / "bin" / "_machine_local.py"

STUB = '''\
import os, subprocess, sys, time

if __name__ == "__main__":
    with open(os.environ["STUB_PIDFILE"], "a") as f:
        f.write(str(os.getpid()) + "\\n")
    if os.environ.get("STUB_GRANDCHILD"):
        gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        with open(os.environ["STUB_GRANDCHILD"], "a") as f:
            f.write(str(gc.pid) + "\\n")
    time.sleep(float(os.environ.get("STUB_SLEEP", "0")))
    print('{"repos.a": "/x/a", "repos.b": null}')
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    reg = tmp_path / "reg"
    reg.mkdir()
    (tmp_path / "settings").mkdir()
    pidfile = tmp_path / "pids.txt"
    stub = tmp_path / "_machine_local.py"
    stub.write_text(STUB, encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setenv(locked_write.MACHINE_LOCK_ROOT_ENV, str(tmp_path / "locks"))
    monkeypatch.setenv("STUB_PIDFILE", str(pidfile))
    monkeypatch.setenv("STUB_SLEEP", "0")
    monkeypatch.setenv("MACHINE_LOCAL_SLOT_DIR", str(tmp_path / "slots"))
    monkeypatch.delenv("MACHINE_LOCAL_MAX_INFLIGHT", raising=False)
    mls._CACHE.clear()
    yield {"reg": reg, "stub": str(stub), "pidfile": pidfile, "tmp": tmp_path}
    mls._CACHE.clear()


def _spawned(pidfile: Path) -> list[int]:
    if not pidfile.exists():
        return []
    return [int(x) for x in pidfile.read_text().split()]


def _wait_for(path: Path, timeout: float = 10.0) -> list[int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pids = _spawned(path)
        if pids:
            return pids
        time.sleep(0.02)
    return []


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        from coordinator_core.bash_guards import _host_probe

        return _host_probe.creation_time(pid) is not None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def test_concurrent_processes_start_exactly_one_child(env, monkeypatch):
    monkeypatch.setenv("STUB_SLEEP", "1.0")
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from coordinator_core import machine_local_spawn as m;"
        "print(m.dump(sys.argv[2], in_process=False))"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(REPO_ROOT), env["stub"]],
            stdout=subprocess.PIPE,
            text=True,
            env=dict(os.environ),
        )
        for _ in range(3)
    ]
    outs = [p.communicate(timeout=60)[0].strip() for p in procs]
    assert len(_spawned(env["pidfile"])) == 1
    assert outs == ["{'repos.a': '/x/a', 'repos.b': None}"] * 3


CALLER = (
    "import sys; sys.path.insert(0, sys.argv[1]);"
    "from coordinator_core import machine_local_spawn as m;"
    "cp = m.run_bounded([sys.executable, sys.argv[2], 'dump']);"
    "print('ran' if cp is not None else 'none')"
)


# A stub must outlast a waiter's slot timeout (so contention is observed) yet finish, with its
# interpreter start, inside the spawn ceiling (so the slot holders are not killed as timeouts).
CONTENDED_STUB_SLEEP_S = mls.ML_WAIT_TIMEOUT_S * 2
assert mls.ML_WAIT_TIMEOUT_S < CONTENDED_STUB_SLEEP_S < mls.ML_SPAWN_TIMEOUT_S


def _live_stub_count(pidfile: Path) -> int:
    return sum(1 for pid in _spawned(pidfile) if _alive(pid))


def _sample_live_max(pidfile: Path, running) -> int:
    peak = 0
    while running():
        peak = max(peak, _live_stub_count(pidfile))
        time.sleep(0.03)
    return max(peak, _live_stub_count(pidfile))


def test_processes_never_exceed_the_box_wide_cap_and_leave_no_survivors(env, monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_MAX_INFLIGHT", "2")
    monkeypatch.setenv("STUB_SLEEP", str(CONTENDED_STUB_SLEEP_S))
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", CALLER, str(REPO_ROOT), env["stub"]],
            stdout=subprocess.PIPE,
            text=True,
            env=dict(os.environ),
        )
        for _ in range(4)
    ]
    peak = _sample_live_max(env["pidfile"], lambda: any(p.poll() is None for p in procs))
    outs = [p.communicate(timeout=60)[0].strip() for p in procs]
    assert peak <= 2
    assert "none" in outs
    assert all(_wait_dead(pid) for pid in _spawned(env["pidfile"]))


@pytest.mark.skipif(
    sys.platform == "darwin", reason="macOS has no parent-death signal or job object"
)
def test_a_killed_caller_leaves_no_surviving_stub_child(env, monkeypatch):
    monkeypatch.setenv("STUB_SLEEP", "30")
    caller = subprocess.Popen(
        [sys.executable, "-c", CALLER, str(REPO_ROOT), env["stub"]],
        stdout=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    )
    pids = _wait_for(env["pidfile"])
    assert pids
    caller.kill()
    caller.wait(10)
    assert all(_wait_dead(pid, timeout=5.0) for pid in pids)


_STORE_TOML = '[stores.alpha]\npath = "{root}"\nowner = "me"\npurpose = "keeps   the\\ttabs"\n'

STORE_STUB = '''\
import os, sys
sys.stdout.write(os.environ.get("STUB_OUT", ""))  # Windows drops a var set to ""
sys.exit(int(os.environ["STUB_RC"]))
'''
