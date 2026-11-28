"""Tests for the spawn-free host probe: RAM trust, liveness keyed on creation time,
snapshot. Platform readers are stubbed; the live checks run against this host's own process."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from coordinator_core.bash_guards import _host_probe as hp
from pathlib import Path


def _stub_mem(monkeypatch, tup) -> None:
    monkeypatch.setattr(hp, "_memory_tuple", lambda: tup)


def test_trusted_reading(monkeypatch: pytest.MonkeyPatch) -> None:
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
def test_untrusted_or_unreadable_reading_is_not_trusted(monkeypatch: pytest.MonkeyPatch, tup) -> None:
    _stub_mem(monkeypatch, tup)
    assert hp.read_available_mb().trusted is False


def test_reader_exception_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(hp, "_memory_tuple", boom)
    r = hp.read_available_mb()
    assert r.avail_mb is None and r.trusted is False


@pytest.mark.parametrize("plat,attr", [
    ("windows", "_windows_memory_mb"), ("darwin", "_darwin_memory_mb"), ("linux", "_posix_memory_mb")])
def test_memory_delegates_to_host_sampler(monkeypatch: pytest.MonkeyPatch, plat, attr) -> None:
    hs = hp._host_sampler()
    monkeypatch.setattr(hp, "_platform", lambda: plat)
    monkeypatch.setattr(hs, attr, lambda: (1, 2, 3))
    assert hp._memory_tuple() == (1, 2, 3)


@pytest.mark.parametrize("plat,name", [
    ("windows", "_windows_alive"), ("darwin", "_darwin_alive"), ("linux", "_linux_alive")])
def test_alive_dispatch_and_exception_reads_dead(monkeypatch: pytest.MonkeyPatch, plat, name) -> None:
    monkeypatch.setattr(hp, "_platform", lambda: plat)
    monkeypatch.setattr(hp, name, lambda pid, ct: (pid, ct) == (5, 9))
    assert hp.alive(5, 9) is True
    assert hp.alive(5, 10) is False

    def boom(pid, ct):
        raise OSError
    monkeypatch.setattr(hp, name, boom)
    assert hp.alive(5, 9) is False


def test_linux_stat_parses_comm_with_spaces_and_parens(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    raw = "42 (my (odd) proc) S 7 42 42 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 98765 0 0\n"
    monkeypatch.setattr("builtins.open", lambda *a, **k: __import__("io").StringIO(raw))
    assert hp._linux_stat(42) == ("my (odd) proc", 7, 98765)


def test_linux_reused_pid_reads_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hp, "_linux_stat", lambda pid: ("x", 1, 500))
    assert hp._linux_alive(10, 500) is True
    assert hp._linux_alive(10, 499) is False


def test_linux_missing_pid_reads_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hp, "_linux_stat", lambda pid: None)
    assert hp._linux_alive(10, 500) is False


def test_darwin_reused_pid_reads_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hp.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(hp, "_darwin_bsdinfo", lambda pid: ("x", 1, 500))
    assert hp._darwin_alive(10, 500) is True
    assert hp._darwin_alive(10, 501) is False


def test_darwin_esrch_dead_eperm_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    def esrch(pid, sig):
        raise ProcessLookupError
    def eperm(pid, sig):
        raise PermissionError
    monkeypatch.setattr(hp, "_darwin_bsdinfo", lambda pid: ("x", 1, 500))
    monkeypatch.setattr(hp.os, "kill", esrch)
    assert hp._darwin_alive(10, 500) is False
    monkeypatch.setattr(hp.os, "kill", eperm)
    assert hp._darwin_alive(10, 500) is True


def test_darwin_snapshot_from_stubbed_libproc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hp, "_darwin_pids", lambda: [1, 2])
    monkeypatch.setattr(hp, "_darwin_bsdinfo", lambda p: None if p == 2 else ("launchd", 0, 7))
    rows = hp._darwin_snapshot()
    assert [(r.pid, r.ppid, r.ctime, r.name) for r in rows] == [(1, 0, 7, "launchd")]


def test_darwin_snapshot_names_native_claude_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hp, "_darwin_pids", lambda: [1, 2, 3])
    names = {1: "2.1.296", 2: "2.1.296", 3: "node"}
    paths = {
        1: "u/.local/share/claude/versions/2.1.296",
        2: "opt/other/versions/2.1.296",
    }
    monkeypatch.setattr(hp, "_darwin_bsdinfo", lambda p: (names[p], 0, p))
    monkeypatch.setattr(hp, "_darwin_exe_path", lambda p: paths[p])
    monkeypatch.setattr(hp, "_darwin_argv", lambda p: ["node", "/srv/app/server.js"])
    assert [r.name for r in hp._darwin_snapshot()] == ["claude", "2.1.296", "node"]


@pytest.mark.parametrize("name", ["1.", ".1", "...", "2", "claude", "2.1.296x"])
def test_looks_like_version_rejects_non_versions(name) -> None:
    assert not hp._looks_like_version(name)


def test_looks_like_version_accepts_semver() -> None:
    assert hp._looks_like_version("2.1.296") and hp._looks_like_version("10.0")


@pytest.mark.parametrize(
    "argv",
    [
        ["node", "/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js"],
        ["node", "/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/cli.js", "--resume"],
        ["node", "C:\\Users\\u\\npm\\node_modules\\@anthropic-ai\\claude-code\\cli.js"],
    ],
)
def test_node_running_the_claude_cli_entry_is_the_anchor(argv) -> None:
    assert hp._named_for_anchor(7, "node", lambda p: None, lambda p: argv) == "claude"


@pytest.mark.parametrize(
    "argv",
    [
        ["node", "/srv/app/server.js"],
        ["node", "/x/@anthropic-ai/claude-code/other.js"],
        ["node"],
        None,
    ],
)
def test_other_node_processes_keep_their_name(argv) -> None:
    assert hp._named_for_anchor(7, "node", lambda p: None, lambda p: argv) == "node"


def test_a_non_node_image_never_pays_the_argv_read() -> None:
    def boom(pid):
        raise AssertionError("argv read")
    assert hp._named_for_anchor(7, "python", lambda p: None, boom) == "python"


def test_linux_snapshot_names_npm_global_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    real_listdir = os.listdir
    monkeypatch.setattr(hp.os, "listdir", lambda d: ["5"] if d == "/proc" else real_listdir(d))
    monkeypatch.setattr(hp, "_linux_stat", lambda pid: ("node", 1, 9))
    monkeypatch.setattr(hp, "_linux_argv", lambda pid: ["node", "/l/node_modules/@anthropic-ai/claude-code/cli.js"])
    assert [r.name for r in hp._linux_snapshot()] == ["claude"]


def test_linux_argv_reads_this_process_when_proc_exists() -> None:
    if not os.path.exists("/proc/self/cmdline"):
        pytest.skip("no procfs")
    assert hp._linux_argv(os.getpid())


@pytest.mark.skipif(sys.platform != "darwin", reason="sysctl KERN_PROCARGS2 is macOS-only")
def test_darwin_argv_reads_this_process() -> None:
    argv = hp._darwin_argv(os.getpid())
    assert argv and argv[0]


@pytest.mark.skipif(os.name == "nt", reason="ctypes.CDLL(None) (dlopen of the process image) has no Windows equivalent")
def test_darwin_libc_handle_is_cached() -> None:
    assert hp._libc() is hp._libc()


def test_linux_snapshot_names_native_claude_install(monkeypatch: pytest.MonkeyPatch) -> None:
    real_listdir = os.listdir
    monkeypatch.setattr(hp.os, "listdir", lambda d: ["5", "self"] if d == "/proc" else real_listdir(d))
    monkeypatch.setattr(hp, "_linux_stat", lambda pid: ("2.1.296", 1, 9))
    monkeypatch.setattr(hp, "_linux_exe_path", lambda pid: "u/.local/share/claude/versions/2.1.296")
    assert [r.name for r in hp._linux_snapshot()] == ["claude"]


def test_version_name_keeps_its_name_when_path_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreadable(pid):
        raise OSError
    assert hp._named_for_anchor(1, "2.1.296", unreadable) == "2.1.296"


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS-only")
def test_darwin_exe_path_reads_this_process() -> None:
    path = hp._darwin_exe_path(os.getpid())
    assert path and os.path.isfile(path)


def test_darwin_unreadable_table_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hp, "_darwin_pids", lambda: None)
    assert hp._darwin_snapshot() is None


def test_unreadable_primitives_return_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hp, "_platform", lambda: "linux")
    monkeypatch.setattr(hp, "_linux_stat", lambda pid: None)
    monkeypatch.setattr(hp, "_linux_snapshot", lambda: None)
    assert hp.creation_time(1) is None
    assert hp.snapshot() is None


def test_snapshot_exception_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom():
        raise OSError
    monkeypatch.setattr(hp, "_platform", lambda: "linux")
    monkeypatch.setattr(hp, "_linux_snapshot", boom)
    assert hp.snapshot() is None


def test_host_primitives_satisfy_protocol() -> None:
    from coordinator_core.bash_guards._heavy_admission_contract import ProcessPrimitives
    assert isinstance(hp.HostPrimitives(), ProcessPrimitives)


def test_host_primitives_expose_reap_surface() -> None:
    prim = hp.HostPrimitives()
    assert prim.cpu_time_s is hp.cpu_time_s
    assert prim.command_line is hp.command_line


def test_linux_cpu_time_sums_utime_stime(monkeypatch: pytest.MonkeyPatch) -> None:
    rest = ["S", "1", "1", "1", "0", "-1", "0", "0", "0", "0", "0", "300", "150"] + ["0"] * 10
    monkeypatch.setattr(hp, "_linux_stat_fields", lambda pid: ("a (b) c", rest))
    monkeypatch.setattr(hp.os, "sysconf", lambda name: 100, raising=False)
    assert hp._linux_cpu_time_s(1) == 4.5


def test_linux_cpu_time_unreadable_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hp, "_linux_stat_fields", lambda pid: None)
    assert hp._linux_cpu_time_s(1) is None
    monkeypatch.setattr(hp, "_linux_stat_fields", lambda pid: ("x", ["S"]))
    assert hp._linux_cpu_time_s(1) is None


def test_cpu_time_reader_exception_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(pid):
        raise RuntimeError("x")

    monkeypatch.setattr(hp, "_platform", lambda: "linux")
    monkeypatch.setattr(hp, "_linux_cpu_time_s", boom)
    assert hp.cpu_time_s(1) is None


# ---- live checks against this host (Windows or Linux; macOS is READ-NOT-EXECUTED) ----

live = pytest.mark.skipif(sys.platform == "darwin", reason="macOS layout unexecuted")


@live
def test_live_cpu_time_of_self_is_non_negative_float() -> None:
    value = hp.cpu_time_s(os.getpid())
    assert isinstance(value, float) and value >= 0.0


@live
def test_live_cpu_time_of_bogus_pid_is_none() -> None:
    assert hp.cpu_time_s(2**22 + 12345) is None


@live
def test_live_command_line_of_self_is_readable() -> None:
    assert hp.command_line(os.getpid())


@live
def test_live_self_alive_and_reused_pid_dead() -> None:
    me = os.getpid()
    ct = hp.creation_time(me)
    assert ct
    assert hp.alive(me, ct) is True
    assert hp.alive(me, ct + 1) is False


@live
def test_live_snapshot_contains_self_and_parent_link() -> None:
    rows = hp.snapshot()
    assert rows
    by_pid = {r.pid: r for r in rows}
    assert by_pid[os.getpid()].ppid == os.getppid()


@live
@pytest.mark.spawns_process
@pytest.mark.cadence
def test_live_dead_pid_reads_dead() -> None:
    p = subprocess.Popen(
        [sys.executable, "-c", "pass"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    ct = hp.creation_time(p.pid)
    p.wait()
    if ct is None:
        pytest.skip("child exited before creation time was read")
    assert hp.alive(p.pid, ct) is False
