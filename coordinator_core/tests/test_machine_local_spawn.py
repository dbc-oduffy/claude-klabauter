"""Tests for coordinator_core.machine_local_spawn: bounded spawn, in-process dump, cache, single-flight."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from coordinator_core import locked_write
from coordinator_core import machine_local_spawn as mls

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


def test_run_bounded_timeout_returns_none_and_reaps_child(env, monkeypatch):
    monkeypatch.setenv("STUB_SLEEP", "30")
    monkeypatch.setattr(mls, "ML_SPAWN_TIMEOUT_S", 1.5)  # outlasts interpreter start so the pid is written
    t0 = time.monotonic()
    result = mls.run_bounded([sys.executable, env["stub"], "dump"])
    assert result is None
    assert time.monotonic() - t0 < 8
    (pid,) = _spawned(env["pidfile"])
    assert _wait_dead(pid)


def test_run_bounded_clamps_a_larger_timeout(env, monkeypatch):
    monkeypatch.setenv("STUB_SLEEP", "30")
    monkeypatch.setattr(mls, "ML_SPAWN_TIMEOUT_S", 0.5)
    t0 = time.monotonic()
    assert mls.run_bounded([sys.executable, env["stub"], "dump"], timeout=60) is None
    assert time.monotonic() - t0 < 8


def test_run_bounded_success_returns_completed_process(env):
    cp = mls.run_bounded([sys.executable, env["stub"], "dump"])
    assert cp is not None and cp.returncode == 0
    assert '"repos.a"' in cp.stdout


def test_run_bounded_unstartable_argv_returns_none(tmp_path):
    assert mls.run_bounded([str(tmp_path / "no-such-binary")]) is None


@pytest.mark.skipif(sys.platform == "win32", reason="group kill is the POSIX path")
def test_run_bounded_timeout_kills_the_grandchild_too(env, monkeypatch):
    gc_file = env["tmp"] / "gc.txt"
    monkeypatch.setenv("STUB_SLEEP", "30")
    monkeypatch.setenv("STUB_GRANDCHILD", str(gc_file))
    monkeypatch.setattr(mls, "ML_SPAWN_TIMEOUT_S", 1.0)
    assert mls.run_bounded([sys.executable, env["stub"], "dump"]) is None
    (gc_pid,) = _wait_for(gc_file)
    assert _wait_dead(gc_pid)


def test_no_second_spawn_after_a_timeout(env, monkeypatch):
    monkeypatch.setenv("STUB_SLEEP", "30")
    monkeypatch.setattr(mls, "ML_SPAWN_TIMEOUT_S", 0.6)
    assert mls.dump(env["stub"], in_process=False) is None
    assert mls.dump(env["stub"], in_process=False) is None
    mls._CACHE.clear()
    assert mls.dump(env["stub"], in_process=False) is None
    assert len(_spawned(env["pidfile"])) == 1


def test_py_impl_dumps_in_process_with_zero_spawns(tmp_path, monkeypatch):
    reg = tmp_path / "reg"
    reg.mkdir()
    (reg / "registry.local.toml").write_text('alpha = "one"\nbeta = ""\n', encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))

    def boom(*a, **k):
        raise AssertionError("dump() spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    payload = mls.dump(str(KERNEL))
    assert payload == {"alpha": "one", "beta": ""}
    assert mls.dump(str(KERNEL), ("--prefix", "alpha")) == {"alpha": "one"}
    assert mls.dump(str(KERNEL), ()) == {"alpha": "one", "beta": ""}


def test_format_json_dump_still_reads_in_process(tmp_path, monkeypatch):
    reg = tmp_path / "reg"
    reg.mkdir()
    (reg / "registry.local.toml").write_text('alpha = "one"\n', encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned"))
    )
    assert mls.dump(str(KERNEL), ("--prefix", "alpha", "--format", "json")) == {"alpha": "one"}
    assert mls._parse_dump_args(("--format", "sh")) is None


def test_in_process_dump_matches_the_spawned_dump(tmp_path, monkeypatch):
    reg = tmp_path / "reg"
    reg.mkdir()
    (reg / "registry.local.toml").write_text('alpha = "one"\nbeta = ""\n', encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setenv(locked_write.MACHINE_LOCK_ROOT_ENV, str(tmp_path / "locks"))
    mls._CACHE.clear()
    in_proc = mls.dump(str(KERNEL))
    spawned = mls.dump(str(KERNEL), in_process=False)
    assert in_proc is not None and in_proc == spawned


def test_get_serves_from_the_dump_and_reads_absence_in_process(tmp_path, monkeypatch):
    reg = tmp_path / "reg"
    reg.mkdir()
    (reg / "registry.local.toml").write_text('alpha = "one"\n', encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned"))
    )
    assert mls.get(str(KERNEL), "alpha") == ("one", True)
    assert mls.get(str(KERNEL), "nothing.here") == (None, True)


def _kernel_env(tmp_path, monkeypatch, toml: str) -> None:
    reg = tmp_path / "reg"
    reg.mkdir()
    (reg / "registry.local.toml").write_text(toml, encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setenv(locked_write.MACHINE_LOCK_ROOT_ENV, str(tmp_path / "locks"))
    mls._CACHE.clear()


def test_get_distinguishes_set_but_empty_from_unset_on_every_path(tmp_path, monkeypatch):
    _kernel_env(tmp_path, monkeypatch, 'alpha = "one"\nbeta = ""\n')
    impl = str(KERNEL)
    # dump branch, in-process then spawned dump
    assert mls.get(impl, "beta") == ("", True)
    mls._CACHE.clear()
    assert mls.get(impl, "beta", in_process=False) == ("", True)
    # a key absent from the dump: in-process get, and the get spawn
    assert mls._get_in_process(impl, "beta") == ("", True)
    assert mls._get_spawned(impl, "beta") == ("", True)
    assert mls._get_in_process(impl, "alpha") == ("one", True)
    assert mls._get_spawned(impl, "alpha") == ("one", True)
    # unset stays None on both
    assert mls._get_in_process(impl, "gamma.none") == (None, True)
    assert mls._get_spawned(impl, "gamma.none") == (None, True)
    assert mls.get(impl, "gamma.none") == (None, True)


def _count_layer_builds(monkeypatch) -> list:
    builds: list = []
    real_load = mls._load_kernel

    def load(impl):
        mod = real_load(impl)
        real_build = mod._build_resolution_layers

        def build(*a, **k):
            builds.append(1)
            return real_build(*a, **k)

        mod._build_resolution_layers = build
        return mod

    monkeypatch.setattr(mls, "_load_kernel", load)
    return builds


def test_twenty_six_gets_over_an_unchanged_registry_dump_once(tmp_path, monkeypatch):
    _kernel_env(tmp_path, monkeypatch, 'alpha = "one"\nbeta = ""\n')
    mls._DUMP_MEMO.clear()
    builds = _count_layer_builds(monkeypatch)
    impl = str(KERNEL)
    for _ in range(26):
        assert mls.get(impl, "alpha") == ("one", True)
    assert len(builds) == 1
    assert mls.get(impl, "beta") == ("", True)
    assert len(builds) == 1
    assert mls.get(impl, "gamma.none") == (None, True)


@pytest.mark.parametrize("layer", ["registry.toml", "registry.local.toml", "net.local.toml"])
def test_touching_any_layer_file_invalidates_the_dump_memo(tmp_path, monkeypatch, layer):
    _kernel_env(tmp_path, monkeypatch, 'alpha = "one"\n')
    reg = tmp_path / "reg"
    (reg / "registry.toml").write_text('concerns = ["net"]\n', encoding="utf-8")
    (reg / "net.local.toml").write_text('[net]\nhost = "a"\n', encoding="utf-8")
    mls._DUMP_MEMO.clear()
    builds = _count_layer_builds(monkeypatch)
    impl = str(KERNEL)
    assert mls.get(impl, "net.host") == ("a", True)
    assert mls.get(impl, "net.host") == ("a", True)
    assert len(builds) == 1
    target = reg / layer
    before = target.stat()
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))
    assert mls.get(impl, "net.host") == ("a", True)
    assert len(builds) == 2
    # a size change with the mtime pinned back also invalidates
    text = target.read_text(encoding="utf-8")
    mtime = target.stat().st_mtime_ns
    target.write_text(text + "# pad\n", encoding="utf-8")
    os.utime(target, ns=(mtime, mtime))
    mls.get(impl, "net.host")
    assert len(builds) == 3
    assert mls.get(impl, "net.host") == ("a", True)
    assert len(builds) == 3


def test_dump_memo_serves_a_copy_and_follows_a_changed_value(tmp_path, monkeypatch):
    _kernel_env(tmp_path, monkeypatch, 'alpha = "one"\n')
    impl = str(KERNEL)
    mls._DUMP_MEMO.clear()
    first = mls.dump(impl)
    first["alpha"] = "mutated"
    assert mls.dump(impl) == {"alpha": "one"}
    local = tmp_path / "reg" / "registry.local.toml"
    local.write_text('alpha = "two"\n', encoding="utf-8")
    assert mls.get(impl, "alpha") == ("two", True)


def test_get_dump_branch_maps_an_unset_none_value_to_none(env):
    assert mls.get(env["stub"], "repos.b", in_process=False) == (None, True)
    assert mls.get(env["stub"], "repos.a", in_process=False) == ("/x/a", True)


def test_get_spawned_strips_only_the_trailing_newline(env, tmp_path):
    impl = tmp_path / "_echo.py"
    impl.write_text('print("  padded  ")\n', encoding="utf-8")
    assert mls._get_spawned(str(impl), "k") == ("  padded  ", True)


def test_repos_reads_agree_in_process_and_spawned(tmp_path, monkeypatch):
    _kernel_env(tmp_path, monkeypatch, 'alpha = "one"\n')
    sibling = tmp_path / "sib"
    sibling.mkdir()
    monkeypatch.setenv("REPO_ZZPIN", str(sibling))
    impl = str(KERNEL)
    in_proc = mls._get_in_process(impl, "repos.zzpin")
    spawned = mls._get_spawned(impl, "repos.zzpin")
    assert in_proc is not None and in_proc[1] is True and in_proc[0]
    assert in_proc == spawned
    monkeypatch.setenv("REPO_ZZPIN", str(tmp_path / "missing"))
    assert mls._get_in_process(impl, "repos.zzpin") is None
    assert mls._get_spawned(impl, "repos.zzpin")[1] is False


def test_kernel_path_honours_settings_home_then_claude_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "sh"))
    assert mls.kernel_path() == str(tmp_path / "sh" / "bin" / "_machine_local.py")
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME")
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "ch"))
    expected = tmp_path / "ch" / ".coordinator-claude-settings" / "bin" / "_machine_local.py"
    assert mls.kernel_path() == str(expected)


def test_get_falls_back_to_one_bounded_spawn(env, monkeypatch):
    monkeypatch.setenv("STUB_SLEEP", "30")
    monkeypatch.setattr(mls, "ML_SPAWN_TIMEOUT_S", 0.6)
    assert mls.get(env["stub"], "repos.zzz", in_process=False) == (None, False)
    assert len(_spawned(env["pidfile"])) == 2  # one dump, one get: each bounded, neither retried
    assert mls.get(env["stub"], "repos.zzz", in_process=False) == (None, False)
    assert len(_spawned(env["pidfile"])) == 2  # the failed get is cached, not re-spawned


def test_concurrent_threads_start_exactly_one_child(env, monkeypatch):
    monkeypatch.setenv("STUB_SLEEP", "0.05")  # late arrivals hit the cache; waiters need not outwait ML_WAIT_TIMEOUT_S
    results: list = []

    def call():
        results.append(mls.dump(env["stub"], in_process=False))

    threads = [threading.Thread(target=call) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(_spawned(env["pidfile"])) == 1
    assert results == [{"repos.a": "/x/a", "repos.b": None}] * 4


def test_waiter_behind_slow_dump_gives_up_inside_wait_ceiling(env, monkeypatch):
    monkeypatch.setenv("STUB_SLEEP", str(mls.ML_WAIT_TIMEOUT_S * 3))  # must outlast the waiters' ceiling
    results: list = []
    threads = [
        threading.Thread(target=lambda: results.append(mls.dump(env["stub"], in_process=False)))
        for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(_spawned(env["pidfile"])) == 1
    assert results.count(None) == 2 and len(results) == 3


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


def test_shared_cache_serves_a_fresh_process_without_spawning(env):
    assert mls.dump(env["stub"], in_process=False) is not None
    mls._CACHE.clear()
    assert mls.dump(env["stub"], in_process=False) == {"repos.a": "/x/a", "repos.b": None}
    assert len(_spawned(env["pidfile"])) == 1


def test_registry_mtime_change_invalidates_both_caches(env):
    (env["reg"] / "registry.toml").write_text('x = "1"\n', encoding="utf-8")
    mls.dump(env["stub"], in_process=False)
    mls.dump(env["stub"], in_process=False)
    assert len(_spawned(env["pidfile"])) == 1
    local = env["reg"] / "registry.local.toml"
    local.write_text('y = "2"\n', encoding="utf-8")
    os.utime(local, ns=(1_000_000_000, 1_000_000_000))
    mls.dump(env["stub"], in_process=False)
    assert len(_spawned(env["pidfile"])) == 2


def test_lock_wait_timeout_returns_none_and_never_spawns(env, monkeypatch):
    monkeypatch.setattr(mls, "ML_SPAWN_TIMEOUT_S", 0.3)
    target = Path(os.path.abspath(str(env["reg"])))
    with locked_write.held_lock(target, timeout=2.0):
        assert mls.dump(env["stub"], in_process=False) is None
    assert _spawned(env["pidfile"]) == []


def test_missing_settings_home_still_reads_through_the_bounded_spawn(env, monkeypatch):
    from coordinator_core import environment

    monkeypatch.setattr(environment, "_settings_home", lambda _env: None)
    assert mls.dump(env["stub"], in_process=False) == {"repos.a": "/x/a", "repos.b": None}
    mls._CACHE.clear()
    assert mls.dump(env["stub"], in_process=False) is not None
    assert len(_spawned(env["pidfile"])) == 2


def test_windows_kill_walks_the_tree_and_still_kills_the_child():
    from coordinator_core.bash_guards._heavy_admission_contract import ProcRow

    rows = [
        ProcRow(pid=10, ppid=1, ctime=5, name="parent"),
        ProcRow(pid=20, ppid=10, ctime=6, name="stub"),
        ProcRow(pid=30, ppid=20, ctime=7, name="grandchild"),
        ProcRow(pid=40, ppid=1, ctime=5, name="unrelated"),
    ]
    killed: list = []

    class Kill:
        def terminate_verified(self, pid, ctime):
            killed.append(pid)
            return True

        def command_line(self, pid, ctime):
            return None

    class FakeProc:
        pid = 20
        direct_kill = False

        def kill(self):
            FakeProc.direct_kill = True

    mls._kill_tree_windows(FakeProc(), snapshot=lambda: rows, kill=Kill())
    assert sorted(killed) == [20, 30]
    assert FakeProc.direct_kill


def test_windows_kill_survives_an_unreadable_process_table():
    class FakeProc:
        pid = 20
        direct_kill = False

        def kill(self):
            FakeProc.direct_kill = True

    mls._kill_tree_windows(FakeProc(), snapshot=lambda: None, kill=object())
    assert FakeProc.direct_kill


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


def test_threads_never_exceed_the_box_wide_cap(env, monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_MAX_INFLIGHT", "2")
    monkeypatch.setenv("STUB_SLEEP", str(CONTENDED_STUB_SLEEP_S))
    results: list = []
    threads = [
        threading.Thread(
            target=lambda: results.append(mls.run_bounded([sys.executable, env["stub"], "dump"]))
        )
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    peak = _sample_live_max(env["pidfile"], lambda: any(t.is_alive() for t in threads))
    assert peak <= 2
    assert len(results) == 8
    assert sum(r is None for r in results) >= 1
    assert sum(r is not None for r in results) >= 1


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


def test_slot_timeout_returns_none_and_spawns_nothing(env, monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_MAX_INFLIGHT", "1")
    state, fd = mls._claim_slot()
    assert state == "held"
    try:
        t0 = time.monotonic()
        assert mls.run_bounded([sys.executable, env["stub"], "dump"]) is None
        assert time.monotonic() - t0 < 5
    finally:
        locked_write._plat_unlock(fd)
        os.close(fd)
    assert _spawned(env["pidfile"]) == []


def test_contended_dump_is_not_cached_as_a_failure(env, monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_MAX_INFLIGHT", "1")
    state, fd = mls._claim_slot()
    assert state == "held"
    try:
        assert mls.dump(env["stub"], in_process=False) is None
        assert mls.get(env["stub"], "repos.a", in_process=False) == (None, False)
    finally:
        locked_write._plat_unlock(fd)
        os.close(fd)
    mls._CACHE.clear()
    assert mls.dump(env["stub"], in_process=False) == {"repos.a": "/x/a", "repos.b": None}


def test_unusable_slot_dir_fails_open(env, monkeypatch):
    blocker = env["tmp"] / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_SLOT_DIR", str(blocker / "slots"))
    cp = mls.run_bounded([sys.executable, env["stub"], "dump"])
    assert cp is not None and cp.returncode == 0


def test_slot_paths_count_and_probe_order_follow_the_shared_contract(env, monkeypatch):
    slots = env["tmp"] / "slots"
    assert mls._slot_dir() == slots
    assert mls._slot_count() == 16
    monkeypatch.setenv("MACHINE_LOCAL_MAX_INFLIGHT", "4")
    assert mls._slot_count() == 4
    monkeypatch.setattr(os, "getpid", lambda: 10)
    assert mls._probe_order(4) == [2, 3, 0, 1]
    state, fd = mls._claim_slot()
    try:
        assert state == "held"
        assert (slots / "slot-2.lock").exists()
    finally:
        locked_write._plat_unlock(fd)
        os.close(fd)
    monkeypatch.delenv("MACHINE_LOCAL_SLOT_DIR")
    monkeypatch.setenv("LOCALAPPDATA", str(env["tmp"] / "lad"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(env["tmp"] / "xdg"))
    root = env["tmp"] / ("lad" if sys.platform == "win32" else "xdg")
    assert mls._slot_dir() == root / "coordinator" / "machine-local-slots"


def test_shared_cache_hit_takes_no_box_lock(env):
    assert mls.dump(env["stub"], in_process=False) is not None
    mls._CACHE.clear()
    target = Path(os.path.abspath(str(env["reg"])))
    with locked_write.held_lock(target, timeout=2.0):
        assert mls.dump(env["stub"], in_process=False) == {"repos.a": "/x/a", "repos.b": None}
    assert len(_spawned(env["pidfile"])) == 1


def _no_popen(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("cached read spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)


def test_cached_reads_answer_from_the_shared_cache_without_spawning(env, monkeypatch):
    assert mls.dump(env["stub"], in_process=False) is not None
    mls._CACHE.clear()
    _no_popen(monkeypatch)
    assert mls.cached_dump(env["stub"]) == {"repos.a": "/x/a", "repos.b": None}
    mls._CACHE.clear()
    assert mls.cached_get(env["stub"], "repos.a") == ("/x/a", True)
    assert mls.cached_get(env["stub"], "repos.b") == (None, True)


def test_cached_reads_return_the_miss_on_an_empty_cache_without_spawning(env, monkeypatch):
    _no_popen(monkeypatch)
    assert mls.cached_dump(env["stub"]) is None
    assert mls.cached_get(env["stub"], "repos.a") == (None, False)
    assert _spawned(env["pidfile"]) == []


def test_cached_get_reads_the_real_kernel_in_process(tmp_path, monkeypatch):
    _kernel_env(tmp_path, monkeypatch, 'alpha = "one"\n')
    _no_popen(monkeypatch)
    assert mls.cached_get(str(KERNEL), "alpha") == ("one", True)
    assert mls.cached_get(str(KERNEL), "gamma.none") == (None, True)
