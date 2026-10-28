"""Tests for the scoped_pytest launcher: allow-list, admission, contained tree, parent-death kill."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
import types
import uuid
from pathlib import Path

import psutil
import pytest

from coordinator_core.testing import scoped_pytest as sp
from coordinator_core.testing import watch_parent

_REPO = Path(__file__).resolve().parents[2]
_GIB = 1 << 30


def _host(avail_gib):
    from coordinator_core.bash_guards import _heavy_command_class as hcc

    status = hcc._load_box_memory_host().MemoryStatus(
        avail_phys=int(avail_gib * _GIB), total_phys=1 << 40, commit_used=0, commit_limit=1 << 50
    )
    log = []

    def admit(peak, st, floor=0):
        need = max(peak * 1.25, floor + peak)
        return types.SimpleNamespace(admit=need <= st.avail_phys, need_avail=need)

    host = types.SimpleNamespace(
        GIB=_GIB,
        admission_file=lambda: "adm",
        read_recorded_peak=lambda path, key: None,
        read_memory_status=lambda: status,
        reserved_bytes=lambda path: 0,
        guard_floor_bytes=lambda: 0,
        admit=admit,
        admission_lock=lambda path: contextlib.nullcontext(),
        register_command=lambda path, key, pid, peak: log.append((key, pid, peak)),
    )
    return host, log


def _no_spawn(monkeypatch):
    def boom(argv):
        raise AssertionError("spawned")

    monkeypatch.setattr(sp, "_spawn", boom)


def test_refused_admission_exits_75_without_spawn(monkeypatch, tmp_path, capsys):
    _no_spawn(monkeypatch)
    monkeypatch.chdir(tmp_path)
    host, _ = _host(avail_gib=0.1)
    rc = sp.main(["--allow", "test_a.py", "--", "test_a.py"], host=host)
    assert rc == sp.EXIT_ADMISSION_REFUSED
    assert "HOLD" in capsys.readouterr().err


def test_out_of_allow_target_exits_64_naming_it(monkeypatch, tmp_path, capsys):
    _no_spawn(monkeypatch)
    monkeypatch.chdir(tmp_path)
    host, _ = _host(avail_gib=99)
    rc = sp.main(["--allow", "test_a.py", "--", "test_a.py", "test_b.py::test_x"], host=host)
    assert rc == sp.EXIT_TARGET_REFUSED
    assert "test_b.py" in capsys.readouterr().err


def test_no_path_target_is_refused(monkeypatch, tmp_path):
    _no_spawn(monkeypatch)
    monkeypatch.chdir(tmp_path)
    host, _ = _host(avail_gib=99)
    assert sp.main(["--allow", "test_a.py", "--", "-k", "foo"], host=host) == sp.EXIT_TARGET_REFUSED


def test_allowed_nodeid_and_directory_pass_the_check(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pkg").mkdir()
    assert sp.check_allowed(["test_a.py"], ["-q", "-k", "foo", "test_a.py::test_x"]) is None
    assert sp.check_allowed(["pkg"], ["pkg/test_z.py"]) is None
    assert "test_z.py" in sp.check_allowed(["pkg2"], ["pkg/test_z.py"])


@pytest.mark.spawns_process
def test_admitted_run_registers_and_returns_pytest_exit_code(tmp_path, monkeypatch):
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    (tmp_path / "test_bad.py").write_text("def test_bad():\n    assert False\n")
    monkeypatch.chdir(tmp_path)
    host, log = _host(avail_gib=99)
    assert sp.main(["--allow", "test_ok.py", "--", "-q", "test_ok.py"], host=host) == 0
    assert log and log[0][0] == sp.ADMIT_KEY
    assert sp.main(["--allow", "test_bad.py", "--", "-q", "test_bad.py"], host=host) == 1


def test_ancestor_chain_records_watch_parent_clock():
    chain = sp.ancestor_chain()
    assert chain and chain[0][0] == os.getppid()
    for pid, created in chain:
        assert created == watch_parent.process_create_time(pid)


def test_watch_parent_fires_on_mismatched_create_time_and_not_on_matching():
    pid, created = sp.ancestor_chain()[0]
    fired = threading.Event()
    watch_parent.watch_parent(pid, created + 1000.0, fired.set, 0.05)
    assert fired.wait(5)
    healthy = threading.Event()
    watch_parent.watch_parent(pid, created, healthy.set, 0.05)
    assert not healthy.wait(0.5)


@pytest.mark.spawns_process
def test_pid_reuse_of_an_ancestor_kills_the_pytest_tree(tmp_path, monkeypatch):
    (tmp_path / "test_sleep.py").write_text("import time\n\ndef test_sleep():\n    time.sleep(120)\n")
    monkeypatch.chdir(tmp_path)
    pid, created = sp.ancestor_chain()[0]
    monkeypatch.setattr(sp, "ancestor_chain", lambda: [(pid, created + 1000.0)])
    monkeypatch.setattr(sp, "WATCH_INTERVAL_S", 0.1)
    exits = []
    monkeypatch.setattr(sp, "_exit", exits.append)
    host, _ = _host(avail_gib=99)
    start = time.monotonic()
    rc = sp.main(["--allow", "test_sleep.py", "--", "-q", "test_sleep.py"], host=host)
    assert rc == sp.EXIT_PARENT_DIED
    assert time.monotonic() - start < 60
    deadline = time.monotonic() + 5
    while not exits and time.monotonic() < deadline:
        time.sleep(0.02)
    assert exits == [sp.EXIT_PARENT_DIED]


_LAUNCHER_DRIVER = """
import contextlib, sys, types
from coordinator_core.testing import scoped_pytest as sp
sp._SHELL_NAMES = sp._SHELL_NAMES | {"python", "python3", "pythonw"}
G = 1 << 30
from coordinator_core.bash_guards import _heavy_command_class as hcc
st = hcc._load_box_memory_host().MemoryStatus(avail_phys=99 * G, total_phys=100 * G, commit_used=0, commit_limit=1 << 50)
host = types.SimpleNamespace(
    GIB=G, admission_file=lambda: "adm", read_recorded_peak=lambda p, k: None,
    read_memory_status=lambda: st, reserved_bytes=lambda p: 0, guard_floor_bytes=lambda: 0,
    admit=lambda peak, s, floor=0: types.SimpleNamespace(admit=True, need_avail=peak),
    admission_lock=lambda p: contextlib.nullcontext(), register_command=lambda *a: None)
sys.exit(sp.main(sys.argv[1:], host=host))
"""

_WRAPPER = """
import subprocess, sys
sys.exit(subprocess.Popen([sys.executable] + sys.argv[1:]).wait())
"""


def _tagged(token):
    out = []
    for p in psutil.process_iter(["cmdline"]):
        try:
            if any(token in a for a in (p.info["cmdline"] or [])):
                out.append(p)
        except psutil.Error:
            pass
    return out


def _pytest_child_running(token):
    for p in _tagged(token):
        cl = p.info["cmdline"] or []
        if "-m" in cl and "pytest" in cl:
            return True
    return False


def _parent_death_survivors(tmp_path, depth):
    """Run wrapper^depth -> launcher -> sleeping pytest; kill only the topmost wrapper; return the
    pids still carrying the unique token after 5s."""
    token = "scopedtok" + uuid.uuid4().hex[:12]
    test_file = tmp_path / ("test_sleep_%s.py" % token)
    test_file.write_text("import time\n\ndef test_sleep():\n    time.sleep(120)\n")
    driver = tmp_path / "driver.py"
    driver.write_text(_LAUNCHER_DRIVER)
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(_WRAPPER)
    cmd = [sys.executable] + [str(wrapper)] * depth + [str(driver), "--allow", test_file.name, "--", "-q", test_file.name]
    env = dict(os.environ, PYTHONPATH=str(_REPO))
    kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    upper = subprocess.Popen(cmd, cwd=tmp_path, env=env, **kw)
    try:
        deadline = time.monotonic() + 30
        while not _pytest_child_running(token):
            assert time.monotonic() < deadline, "pytest child never started"
            time.sleep(0.2)
        upper.kill()
        deadline = time.monotonic() + 5
        while _tagged(token) and time.monotonic() < deadline:
            time.sleep(0.2)
        return [p.pid for p in _tagged(token)]
    finally:
        for p in _tagged(token):
            with contextlib.suppress(psutil.Error):
                p.kill()
        upper.kill()


@pytest.mark.spawns_process
@pytest.mark.parametrize("depth", [1, 2])
def test_parent_death_kills_the_pytest_tree(tmp_path, depth):
    assert _parent_death_survivors(tmp_path, depth) == []
