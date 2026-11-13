"""cross-repo-memo.py reads machine-local only through the bounded helper: a hung stub never leaves a live child."""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

from coordinator_core import locked_write
from coordinator_core import machine_local_spawn as mls

CLI = Path(__file__).resolve().parents[1] / "cross-repo-memo.py"

STUB = '''\
import os, sys, time

if __name__ == "__main__":
    with open(os.environ["STUB_PIDFILE"], "a") as f:
        f.write(sys.argv[1] + " " + str(os.getpid()) + "\\n")
    time.sleep(float(os.environ.get("STUB_SLEEP", "0")))
    if sys.argv[1] == "dump":
        print('{"repos.a": "/x/a", "repos.b": null}')
    elif sys.argv[1] == "keys":
        print("repos.a")
    else:
        print("/x/a")
'''


@pytest.fixture
def memo(tmp_path, monkeypatch):
    stub = tmp_path / "_machine_local.py"
    stub.write_text(STUB, encoding="utf-8")
    (tmp_path / "reg").mkdir()
    (tmp_path / "settings").mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(stub))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "reg"))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setenv(locked_write.MACHINE_LOCK_ROOT_ENV, str(tmp_path / "locks"))
    monkeypatch.setenv("STUB_PIDFILE", str(tmp_path / "pids.txt"))
    monkeypatch.setenv("STUB_SLEEP", "30")
    monkeypatch.setattr(mls, "ML_SPAWN_TIMEOUT_S", 0.6)
    mls._CACHE.clear()
    sys.path.insert(0, str(CLI.parent))
    spec = importlib.util.spec_from_file_location("cross_repo_memo_under_test", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._MACHINE_LOCAL_CACHE.clear()
    yield mod, tmp_path / "pids.txt"
    mls._CACHE.clear()
    sys.path.remove(str(CLI.parent))


def _spawns(pidfile: Path) -> list[tuple[str, int]]:
    if not pidfile.exists():
        return []
    rows = [line.split() for line in pidfile.read_text().splitlines() if line.strip()]
    return [(verb, int(pid)) for verb, pid in rows]


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


def test_hung_dump_falls_back_to_none_and_leaves_no_live_child(memo):
    mod, pidfile = memo
    assert mod._machine_local_dump() is None
    spawns = _spawns(pidfile)
    assert [v for v, _ in spawns] == ["dump"]
    assert all(_wait_dead(pid) for _, pid in spawns)


def test_hung_dump_is_remembered_not_respawned(memo):
    mod, pidfile = memo
    assert mod._machine_local_dump() is None
    assert mod._machine_local_dump() is None
    assert [v for v, _ in _spawns(pidfile)] == ["dump"]


def test_hung_get_returns_failure_triple_and_leaves_no_live_child(memo):
    mod, pidfile = memo
    assert mod._machine_local_get_detail("repos.a") == (None, False, "")
    spawns = _spawns(pidfile)
    assert [v for v, _ in spawns] == ["dump", "get"]
    assert all(_wait_dead(pid) for _, pid in spawns)
    assert mod._machine_local_get_detail("repos.a") == (None, False, "")
    assert len(_spawns(pidfile)) == 2


def test_hung_keys_returns_none_and_leaves_no_live_child(memo):
    mod, pidfile = memo
    assert mod._machine_local_repos_keys() is None
    spawns = _spawns(pidfile)
    assert [v for v, _ in spawns] == ["dump", "keys"]
    assert all(_wait_dead(pid) for _, pid in spawns)


def test_healthy_stub_still_answers_from_the_dump(memo, monkeypatch):
    mod, pidfile = memo
    monkeypatch.setenv("STUB_SLEEP", "0")
    assert mod._machine_local_get_detail("repos.a") == ("/x/a", True, "")
    assert mod._machine_local_get_detail("repos.b") == (None, True, "")
    assert mod._machine_local_repos_keys() == ["repos.a", "repos.b"]
    assert [v for v, _ in _spawns(pidfile)] == ["dump"]
