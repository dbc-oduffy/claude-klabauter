"""Tests for the spawn-free host probe: RAM trust, liveness keyed on creation time,
snapshot. Platform readers are stubbed; the live checks run against this host's own process."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from coordinator_core.bash_guards import _host_probe as hp


def _stub_mem(monkeypatch, tup):
    monkeypatch.setattr(hp, "_memory_tuple", lambda: tup)


def test_trusted_reading(monkeypatch):
    _stub_mem(monkeypatch, (1000, 7000, 8000))
    r = hp.read_available_mb()
    assert (r.avail_mb, r.trusted) == (7000, True)


@pytest.mark.parametrize("tup", [
    (None, None, None),
    (0, 0, 8000),
    (0, -5, 8000),
    (0, 9000, 8000),
    (0, 100, None),
    (0, 100, 0),
])
def test_untrusted_or_unreadable_reading_is_not_trusted(monkeypatch, tup):
    _stub_mem(monkeypatch, tup)
    assert hp.read_available_mb().trusted is False


def test_reader_exception_is_unreadable(monkeypatch):
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(hp, "_memory_tuple", boom)
    r = hp.read_available_mb()
    assert r.avail_mb is None and r.trusted is False


@pytest.mark.parametrize("plat,attr", [
    ("windows", "_windows_memory_mb"), ("darwin", "_darwin_memory_mb"), ("linux", "_posix_memory_mb")])
def test_memory_delegates_to_host_sampler(monkeypatch, plat, attr):
    hs = hp._host_sampler()
    monkeypatch.setattr(hp, "_platform", lambda: plat)
    monkeypatch.setattr(hs, attr, lambda: (1, 2, 3))
    assert hp._memory_tuple() == (1, 2, 3)


@pytest.mark.parametrize("plat,name", [
    ("windows", "_windows_alive"), ("darwin", "_darwin_alive"), ("linux", "_linux_alive")])
def test_alive_dispatch_and_exception_reads_dead(monkeypatch, plat, name):
    monkeypatch.setattr(hp, "_platform", lambda: plat)
    monkeypatch.setattr(hp, name, lambda pid, ct: (pid, ct) == (5, 9))
    assert hp.alive(5, 9) is True
    assert hp.alive(5, 10) is False

    def boom(pid, ct):
        raise OSError
    monkeypatch.setattr(hp, name, boom)
    assert hp.alive(5, 9) is False


def test_linux_stat_parses_comm_with_spaces_and_parens(monkeypatch, tmp_path):
    raw = "42 (my (odd) proc) S 7 42 42 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 98765 0 0\n"
    monkeypatch.setattr("builtins.open", lambda *a, **k: __import__("io").StringIO(raw))
    assert hp._linux_stat(42) == ("my (odd) proc", 7, 98765)


def test_linux_reused_pid_reads_dead(monkeypatch):
    monkeypatch.setattr(hp, "_linux_stat", lambda pid: ("x", 1, 500))
    assert hp._linux_alive(10, 500) is True
    assert hp._linux_alive(10, 499) is False


def test_linux_missing_pid_reads_dead(monkeypatch):
    monkeypatch.setattr(hp, "_linux_stat", lambda pid: None)
    assert hp._linux_alive(10, 500) is False


def test_darwin_reused_pid_reads_dead(monkeypatch):
    monkeypatch.setattr(hp.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(hp, "_darwin_bsdinfo", lambda pid: ("x", 1, 500))
    assert hp._darwin_alive(10, 500) is True
    assert hp._darwin_alive(10, 501) is False


def test_darwin_esrch_dead_eperm_alive(monkeypatch):
    def esrch(pid, sig):
        raise ProcessLookupError
    def eperm(pid, sig):
        raise PermissionError
    monkeypatch.setattr(hp, "_darwin_bsdinfo", lambda pid: ("x", 1, 500))
    monkeypatch.setattr(hp.os, "kill", esrch)
    assert hp._darwin_alive(10, 500) is False
    monkeypatch.setattr(hp.os, "kill", eperm)
    assert hp._darwin_alive(10, 500) is True


def test_darwin_snapshot_from_stubbed_libproc(monkeypatch):
    monkeypatch.setattr(hp, "_darwin_pids", lambda: [1, 2])
    monkeypatch.setattr(hp, "_darwin_bsdinfo", lambda p: None if p == 2 else ("launchd", 0, 7))
    rows = hp._darwin_snapshot()
    assert [(r.pid, r.ppid, r.ctime, r.name) for r in rows] == [(1, 0, 7, "launchd")]


def test_darwin_snapshot_names_native_claude_install(monkeypatch):
    monkeypatch.setattr(hp, "_darwin_pids", lambda: [1, 2, 3])
    names = {1: "2.1.296", 2: "2.1.296", 3: "node"}
    paths = {
        1: "u/.local/share/claude/versions/2.1.296",
        2: "opt/other/versions/2.1.296",
    }
    monkeypatch.setattr(hp, "_darwin_bsdinfo", lambda p: (names[p], 0, p))
    monkeypatch.setattr(hp, "_darwin_exe_path", lambda p: paths[p])
    assert [r.name for r in hp._darwin_snapshot()] == ["claude", "2.1.296", "node"]


def test_linux_snapshot_names_native_claude_install(monkeypatch):
    real_listdir = os.listdir
    monkeypatch.setattr(hp.os, "listdir", lambda d: ["5", "self"] if d == "/proc" else real_listdir(d))
    monkeypatch.setattr(hp, "_linux_stat", lambda pid: ("2.1.296", 1, 9))
    monkeypatch.setattr(hp, "_linux_exe_path", lambda pid: "u/.local/share/claude/versions/2.1.296")
    assert [r.name for r in hp._linux_snapshot()] == ["claude"]


def test_version_name_keeps_its_name_when_path_unreadable(monkeypatch):
    def unreadable(pid):
        raise OSError
    assert hp._named_for_anchor(1, "2.1.296", unreadable) == "2.1.296"


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS-only")
def test_darwin_exe_path_reads_this_process():
    path = hp._darwin_exe_path(os.getpid())
    assert path and os.path.isfile(path)


def test_darwin_unreadable_table_is_none(monkeypatch):
    monkeypatch.setattr(hp, "_darwin_pids", lambda: None)
    assert hp._darwin_snapshot() is None


def test_unreadable_primitives_return_none(monkeypatch):
    monkeypatch.setattr(hp, "_platform", lambda: "linux")
    monkeypatch.setattr(hp, "_linux_stat", lambda pid: None)
    monkeypatch.setattr(hp, "_linux_snapshot", lambda: None)
    assert hp.creation_time(1) is None
    assert hp.snapshot() is None


def test_snapshot_exception_is_none(monkeypatch):
    def boom():
        raise OSError
    monkeypatch.setattr(hp, "_platform", lambda: "linux")
    monkeypatch.setattr(hp, "_linux_snapshot", boom)
    assert hp.snapshot() is None


def test_host_primitives_satisfy_protocol():
    from coordinator_core.bash_guards._heavy_admission_contract import ProcessPrimitives
    assert isinstance(hp.HostPrimitives(), ProcessPrimitives)


# ---- live checks against this host (Windows or Linux; macOS is READ-NOT-EXECUTED) ----

live = pytest.mark.skipif(sys.platform == "darwin", reason="macOS layout unexecuted")


@live
def test_live_self_alive_and_reused_pid_dead():
    me = os.getpid()
    ct = hp.creation_time(me)
    assert ct
    assert hp.alive(me, ct) is True
    assert hp.alive(me, ct + 1) is False


@live
def test_live_snapshot_contains_self_and_parent_link():
    rows = hp.snapshot()
    assert rows
    by_pid = {r.pid: r for r in rows}
    assert by_pid[os.getpid()].ppid == os.getppid()


@live
@pytest.mark.spawns_process
def test_live_dead_pid_reads_dead():
    p = subprocess.Popen(
        [sys.executable, "-c", "pass"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    ct = hp.creation_time(p.pid)
    p.wait()
    if ct is None:
        pytest.skip("child exited before creation time was read")
    assert hp.alive(p.pid, ct) is False
