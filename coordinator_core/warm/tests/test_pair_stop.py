"""pair_stop: cmdline match and stop order, against a fake psutil (no real process is signalled)."""

from __future__ import annotations

import os
import sys
import threading

import pytest

from coordinator_core.warm import breadcrumb, pair_stop

SUP = ("coordinator_core", "warm", "supervisor.py")
SRV = ("coordinator_core", "warm", "server.py")


class _NoSuchProcess(Exception):
    pass


class _AccessDenied(Exception):
    pass


class _Proc:
    def __init__(self, world, pid, name, argv, stubborn=False):
        self.world = world
        self.pid = pid
        self.info = {"pid": pid, "name": name}
        self._argv = argv
        self.stubborn = stubborn
        self.alive = True

    def cmdline(self):
        return list(self._argv)

    def terminate(self):
        self.world.log.append(("terminate", self.pid))
        if not self.stubborn:
            self.alive = False

    def kill(self):
        self.world.log.append(("kill", self.pid))
        # A stubborn process survives kill too, so a survivor is reportable.

    def is_running(self):
        return self.alive


class _World:
    NoSuchProcess = _NoSuchProcess
    AccessDenied = _AccessDenied

    def __init__(self):
        self.procs = []
        self.log = []

    def add(self, pid, name, argv, stubborn=False):
        proc = _Proc(self, pid, name, argv, stubborn)
        self.procs.append(proc)
        return proc

    def process_iter(self, attrs=None):
        return [p for p in self.procs if p.alive]

    def Process(self, pid):
        for p in self.procs:
            if p.pid == pid and p.alive:
                return p
        raise _NoSuchProcess(pid)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(pair_stop, "_TERMINATE_GRACE_SECS", 0.02)
    monkeypatch.setattr(pair_stop, "_TERMINATE_POLL_INTERVAL_SECS", 0.005)
    monkeypatch.setattr(pair_stop, "_SETTLE_SECS", 0.05)
    monkeypatch.setattr(pair_stop, "_SETTLE_MAX_SECS", 1.0)


def _script(root, parts):
    return str(root.joinpath(*parts))


def test_supervisor_terminated_before_server(tmp_path):
    root = tmp_path / "mirror"
    w = _World()
    w.add(20, "python.exe", [sys.executable, _script(root, SRV)])
    w.add(10, "python.exe", [sys.executable, _script(root, SUP)])
    result = pair_stop.stop_warm_pair(root, psutil_module=w)
    signalled = [pid for kind, pid in w.log if kind == "terminate"]
    assert signalled == [10, 20]
    assert result.ok() and {p.role for p in result.stopped} == {"supervisor", "server"}


def test_survivor_after_kill_fails_and_is_rendered(tmp_path):
    root = tmp_path / "mirror"
    w = _World()
    w.add(30, "python.exe", [sys.executable, _script(root, SRV)], stubborn=True)
    result = pair_stop.stop_warm_pair(root, psutil_module=w)
    assert not result.ok()
    assert ("kill", 30) in w.log
    assert [p.pid for p in result.survivors] == [30]
    line = result.render_survivors()
    assert "pid=30" in line and "role=server" in line


def test_multi_root_survivor_releases_every_held_marker(tmp_path, monkeypatch):
    from contextlib import ExitStack

    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "rt"))
    roots = [tmp_path / "a", tmp_path / "b"]
    for r in roots:
        r.mkdir()
    w = _World()
    w.add(40, "python.exe", [sys.executable, _script(roots[1], SRV)], stubborn=True)
    stack = ExitStack()
    refused = None
    for r in roots:
        stack.enter_context(breadcrumb.hold_publish_marker(r, holder="t"))
        result = pair_stop.stop_warm_pair(r, psutil_module=w)
        if not result.ok():
            refused = r
            stack.close()
            break
    assert refused == roots[1]
    for r in roots:
        assert not breadcrumb.publish_marker_path(r).exists()


def test_nothing_alive_is_ok_and_empty(tmp_path):
    w = _World()
    result = pair_stop.stop_warm_pair(tmp_path / "mirror", psutil_module=w)
    assert result.ok() and result.stopped == () and w.log == []


def test_graceful_ask_used_for_verified_server_record(tmp_path, monkeypatch):
    root = tmp_path / "mirror"
    w = _World()
    srv = w.add(40, "python.exe", [sys.executable, _script(root, SRV)])
    monkeypatch.setattr(
        breadcrumb, "read_breadcrumb", lambda r=None: {"pid": 40, "pipe": "p", "stable_pid_start_epoch": 1}
    )
    monkeypatch.setattr(pair_stop, "stable_pid_alive", lambda pid, stored_start_epoch="": srv.alive)
    asked = []

    def ask(pipe):
        asked.append(pipe)
        srv.alive = False
        return True

    result = pair_stop.stop_warm_pair(root, psutil_module=w, ask_server=ask)
    assert asked == ["p"] and result.ok()
    assert w.log == []


class _Pipe:
    def __init__(self, reply):
        self.reply = reply
        self.released = threading.Event()

    def write(self, data):
        return len(data)

    def flush(self):
        pass

    def readline(self):
        if self.reply is None:
            self.released.wait(5)
            return b""
        return self.reply

    def close(self):
        self.released.set()


def test_unanswered_ask_is_retried_then_graced_then_terminated(tmp_path, monkeypatch):
    root = tmp_path / "mirror"
    w = _World()
    srv = w.add(40, "python.exe", [sys.executable, _script(root, SRV)])
    monkeypatch.setattr(
        breadcrumb, "read_breadcrumb", lambda r=None: {"pid": 40, "pipe": "p", "stable_pid_start_epoch": 1}
    )
    monkeypatch.setattr(pair_stop, "stable_pid_alive", lambda pid, stored_start_epoch="": srv.alive)
    monkeypatch.setattr(pair_stop, "_confirm_grace_secs", lambda: 0.2)
    asks = []

    def ask(pipe):
        asks.append(pipe)
        return False

    started = pair_stop.time.monotonic()
    result = pair_stop.stop_warm_pair(root, psutil_module=w, ask_server=ask)
    assert asks == ["p", "p"]
    assert pair_stop.time.monotonic() - started >= 0.2
    assert ("terminate", 40) in w.log and result.ok()


def test_unanswered_ask_is_not_terminated_while_server_exits_within_grace(tmp_path, monkeypatch):
    root = tmp_path / "mirror"
    w = _World()
    srv = w.add(40, "python.exe", [sys.executable, _script(root, SRV)])
    monkeypatch.setattr(
        breadcrumb, "read_breadcrumb", lambda r=None: {"pid": 40, "pipe": "p", "stable_pid_start_epoch": 1}
    )
    monkeypatch.setattr(pair_stop, "stable_pid_alive", lambda pid, stored_start_epoch="": srv.alive)
    monkeypatch.setattr(pair_stop, "_confirm_grace_secs", lambda: 5.0)
    threading.Timer(0.1, lambda: setattr(srv, "alive", False)).start()
    result = pair_stop.stop_warm_pair(root, psutil_module=w, ask_server=lambda _pipe: False)
    assert w.log == [] and result.ok()


def test_confirm_grace_derives_from_the_drain_ceiling():
    from coordinator_core.warm import lifecycle

    assert pair_stop._confirm_grace_secs() == lifecycle._drain_ceiling_secs() + pair_stop._CONFIRM_GRACE_MARGIN_SECS
    assert pair_stop._confirm_grace_secs() > lifecycle._drain_ceiling_secs()


def test_answered_ask_is_asked(monkeypatch):
    monkeypatch.setattr(pair_stop, "_open_pipe", lambda pipe: _Pipe(b'{"id": 1}\n'))
    assert pair_stop._ask_server_to_stop("p") is True
    monkeypatch.setattr(pair_stop, "_ASK_READ_DEADLINE_SECS", 0.05)
    monkeypatch.setattr(pair_stop, "_open_pipe", lambda pipe: _Pipe(None))
    assert pair_stop._ask_server_to_stop("p") is False


def test_dispatch_timeout_reply_is_not_an_acknowledgement(monkeypatch):
    wedged = b'{"jsonrpc": "2.0", "id": 1, "error": {"code": -32004, "message": "t"}}\n'
    monkeypatch.setattr(pair_stop, "_open_pipe", lambda pipe: _Pipe(wedged))
    assert pair_stop._ask_server_to_stop("p") is False
    other = b'{"jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": "skew"}}\n'
    monkeypatch.setattr(pair_stop, "_open_pipe", lambda pipe: _Pipe(other))
    assert pair_stop._ask_server_to_stop("p") is True


def _spawn_argv(parent):
    return [
        sys.executable, "-c",
        f"from multiprocessing.spawn import spawn_main; spawn_main(parent_pid={parent}, pipe_handle=9)",
        "--multiprocessing-fork",
    ]


def test_orphaned_pool_children_of_the_server_are_taken_down(tmp_path):
    root = tmp_path / "mirror"
    w = _World()
    w.add(40, "python.exe", [sys.executable, _script(root, SRV)])
    kids = [w.add(pid, "pythonw.exe", _spawn_argv(40)) for pid in (91, 92)]
    other = w.add(93, "pythonw.exe", _spawn_argv(400))
    result = pair_stop.stop_warm_pair(root, psutil_module=w)
    assert result.ok()
    assert not any(k.alive for k in kids)
    assert other.alive
    assert {("terminate", 91), ("terminate", 92)} <= set(w.log)


def test_stubborn_pool_child_is_a_survivor(tmp_path):
    root = tmp_path / "mirror"
    w = _World()
    w.add(40, "python.exe", [sys.executable, _script(root, SRV)])
    w.add(91, "pythonw.exe", _spawn_argv(40), stubborn=True)
    result = pair_stop.stop_warm_pair(root, psutil_module=w)
    assert not result.ok()
    assert [(p.pid, p.role) for p in result.survivors] == [(91, "pool_child")]


def test_match_is_exact_element_and_python_only(tmp_path):
    root = tmp_path / "mirror"
    sibling = tmp_path / "mirror-old"
    w = _World()
    w.add(1, "python.exe", [sys.executable, _script(root, SRV)])
    w.add(2, "python.exe", [sys.executable, _script(sibling, SRV)])
    w.add(3, "notepad.exe", [_script(root, SRV)])
    w.add(4, "python.exe", [sys.executable, "-m", "coordinator_core.warm.server"])
    w.add(5, "Python3", [sys.executable, _script(root, SUP), "--x"])
    found = pair_stop.find_pair_processes(root, psutil_module=w)
    assert sorted((p.pid, p.role) for p in found) == [(1, "server"), (5, "supervisor")]


def test_cmdline_case_and_separator_folding(tmp_path):
    root = tmp_path / "Mirror"
    w = _World()
    exotic = _script(root, SRV).replace("/", "\\").upper()
    w.add(7, "python.exe", [sys.executable, exotic])
    found = pair_stop.find_pair_processes(tmp_path / "mirror", psutil_module=w)
    if os.name == "nt":
        assert [p.pid for p in found] == [7]
    else:
        assert found == []
        exact = _script(root, SRV)
        w.add(8, "python3", [sys.executable, exact])
        assert [p.pid for p in pair_stop.find_pair_processes(root, psutil_module=w)] == [8]


FD = ("coordinator_core", "warm", "front_door.py")


def test_order_is_supervisor_then_server_asked_then_front_door_asked(tmp_path, monkeypatch):
    from coordinator_core.warm import front_door

    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "rt"))
    root = tmp_path / "mirror"
    w = _World()
    srv = w.add(30, "python.exe", [sys.executable, _script(root, SRV)])
    door = w.add(20, "python.exe", [sys.executable, _script(root, FD)])
    w.add(10, "python.exe", [sys.executable, _script(root, SUP)])
    monkeypatch.setattr(
        breadcrumb, "read_breadcrumb", lambda r=None: {"pid": 30, "pipe": "p", "stable_pid_start_epoch": 1}
    )
    monkeypatch.setattr(front_door, "read_discovery", lambda r=None: {"pid": 20, "port": 1})
    monkeypatch.setattr(
        pair_stop,
        "stable_pid_alive",
        lambda pid, stored_start_epoch="": {30: srv.alive, 20: door.alive}.get(pid, False),
    )
    events = []

    def ask_server(pipe):
        events.append(("ask_server", [k for k, _ in w.log]))
        srv.alive = False
        return True

    def ask_door(record):
        events.append(("ask_door", [k for k, _ in w.log], srv.alive))
        door.alive = False
        return True

    result = pair_stop.stop_warm_pair(root, psutil_module=w, ask_server=ask_server, ask_front_door=ask_door)
    assert events == [("ask_server", ["terminate"]), ("ask_door", ["terminate"], False)]
    assert [pid for kind, pid in w.log if kind == "terminate"] == [10]
    assert result.ok() and {p.role for p in result.stopped} == {"supervisor", "front_door", "server"}


def test_front_door_terminated_only_when_its_ask_fails(tmp_path, monkeypatch):
    from coordinator_core.warm import front_door

    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "rt"))
    root = tmp_path / "mirror"
    w = _World()
    door = w.add(20, "python.exe", [sys.executable, _script(root, FD)])
    monkeypatch.setattr(front_door, "read_discovery", lambda r=None: {"pid": 20, "port": 1})
    monkeypatch.setattr(pair_stop, "stable_pid_alive", lambda pid, stored_start_epoch="": door.alive)
    result = pair_stop.stop_warm_pair(root, psutil_module=w, ask_front_door=lambda _rec: False)
    assert ("terminate", 20) in w.log and result.ok()


def test_stubborn_front_door_is_a_rendered_survivor(tmp_path, monkeypatch):
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "rt"))
    root = tmp_path / "mirror"
    w = _World()
    w.add(21, "python.exe", [sys.executable, _script(root, FD)], stubborn=True)
    result = pair_stop.stop_warm_pair(root, psutil_module=w)
    assert not result.ok()
    assert "pid=21" in result.render_survivors() and "role=front_door" in result.render_survivors()


def test_front_door_discovery_unlinked_only_when_ok(tmp_path, monkeypatch):
    from coordinator_core.warm import front_door

    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "rt"))
    root = tmp_path / "mirror"
    rec = front_door.discovery_path(root)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text("{}", encoding="utf-8")

    w = _World()
    w.add(22, "python.exe", [sys.executable, _script(root, SRV)], stubborn=True)
    assert not pair_stop.stop_warm_pair(root, psutil_module=w).ok()
    assert rec.exists()

    assert pair_stop.stop_warm_pair(root, psutil_module=_World()).ok()
    assert not rec.exists()


def _alias_of(real, alias):
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(real), str(alias))
    else:
        os.symlink(real, alias, target_is_directory=True)


def test_alias_root_matches_real_root_argv_and_reverse(tmp_path, monkeypatch):
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "rt"))
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    _alias_of(real, alias)

    w = _World()
    w.add(50, "python.exe", [sys.executable, _script(real, SRV)])
    assert [p.pid for p in pair_stop.find_pair_processes(alias, psutil_module=w)] == [50]

    w2 = _World()
    w2.add(51, "python.exe", [sys.executable, _script(alias, SRV)])
    assert [p.pid for p in pair_stop.find_pair_processes(real, psutil_module=w2)] == [51]
    assert pair_stop.stop_warm_pair(alias, psutil_module=w).ok()
    assert ("terminate", 50) in w.log


_ROLE_PARTS = {"supervisor": SUP, "front_door": FD, "server": SRV}


def _filter_shapes(real, alias, role):
    script = _script(real, _ROLE_PARTS[role])
    exe = sys.executable
    shapes = {
        "a": ([exe, script], True),
        "b": ([exe, "-S", script, "--root", str(alias)], True),
        "c": ([exe, _script(alias, _ROLE_PARTS[role]), str(real)], True),
        "d": ([exe, "-m", f"coordinator_core.warm.{role}", str(real)], False),
        "e": ([exe, "/".join(_ROLE_PARTS[role]), str(real)], False),
    }
    if os.name == "nt":
        shapes["f"] = ([exe, script[: -len(".py")] + ".PY"], True)
    return shapes


@pytest.mark.parametrize("role", ["supervisor", "front_door", "server"])
@pytest.mark.parametrize("shape", ["a", "b", "c", "d", "e", "f"])
def test_py_filter_drops_no_match(tmp_path, role, shape):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    _alias_of(real, alias)
    shapes = _filter_shapes(real, alias, role)
    if shape not in shapes:
        pytest.skip("shape f is Windows only")
    argv, expect_match = shapes[shape]

    w = _World()
    w.add(70, "python.exe", argv)
    got = {p.role for p in pair_stop.find_pair_processes(real, psutil_module=w)}

    unfiltered = {pair_stop._norm(a) for a in argv}
    reference = {
        r for r, target in pair_stop._role_targets(real).items() if target in unfiltered
    }
    assert got == reference
    assert got == ({role} if expect_match else set())


def test_relative_argv_element_does_not_match_even_in_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "rt"))
    monkeypatch.chdir(tmp_path)
    w = _World()
    w.add(60, "python.exe", [sys.executable, "coordinator_core/warm/server.py"])
    assert pair_stop.find_pair_processes(tmp_path, psutil_module=w) == []


def test_duplicate_servers_one_appearing_mid_stop_are_all_stopped(tmp_path):
    root = tmp_path / "mirror"
    w = _World()
    for pid in (30, 31):
        w.add(pid, "python.exe", [sys.executable, _script(root, SRV)])
    first = w.procs[0]
    original = first.terminate

    def terminate_and_respawn():
        original()
        w.add(32, "python.exe", [sys.executable, _script(root, SRV)])

    first.terminate = terminate_and_respawn
    result = pair_stop.stop_warm_pair(root, psutil_module=w)
    assert result.ok()
    assert not any(p.alive for p in w.procs)
    assert {("terminate", 30), ("terminate", 31), ("terminate", 32)} <= set(w.log)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_real_duplicate_servers_stopped_from_foreign_cwd(tmp_path, monkeypatch):
    import subprocess
    import time

    psutil = pytest.importorskip("psutil")
    root = tmp_path / "engine"
    script = root.joinpath(*SRV)
    script.parent.mkdir(parents=True)
    script.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()
    monkeypatch.chdir(foreign)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    procs = [
        subprocess.Popen([sys.executable, str(script)], cwd=str(foreign), creationflags=flags)
        for _ in range(2)
    ]
    try:
        deadline = time.monotonic() + 10
        while len(pair_stop.find_pair_processes(root)) < 2 and time.monotonic() < deadline:
            time.sleep(0.1)
        assert len(pair_stop.find_pair_processes(root)) == 2
        result = pair_stop.stop_warm_pair(root, ask_server=lambda _pipe: False)
        assert result.ok(), result.render_survivors()
        assert pair_stop.find_pair_processes(root) == []
        assert not any(psutil.pid_exists(p.pid) and p.poll() is None for p in procs)
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=10)


def test_survivor_rendering_names_why(tmp_path):
    root = tmp_path / "mirror"
    w = _World()
    w.add(30, "python.exe", [sys.executable, _script(root, SRV)], stubborn=True)
    w.add(31, "python.exe", [sys.executable, _script(root, SRV)], stubborn=True)
    result = pair_stop.stop_warm_pair(root, psutil_module=w)
    lines = result.render_survivors().splitlines()
    assert len(lines) == 2
    assert all("why=still running after terminate and kill" in ln for ln in lines)


def test_access_denied_process_is_skipped(tmp_path):
    w = _World()
    bad = w.add(9, "python.exe", [])

    def boom():
        raise _AccessDenied()

    bad.cmdline = boom
    assert pair_stop.find_pair_processes(tmp_path, psutil_module=w) == []


def test_probe_lists_access_denied_pids_and_signals_nothing(tmp_path):
    root = tmp_path / "mirror"
    w = _World()
    w.add(40, "python.exe", [sys.executable, _script(root, SRV)])
    locked = w.add(41, "python.exe", [sys.executable, _script(root, SUP)])

    def denied():
        raise _AccessDenied()

    original = w.Process
    w.Process = lambda pid: denied() if pid == 41 else original(pid)
    assert pair_stop.probe_pair_terminable(root, psutil_module=w) == [(41, "supervisor", "access denied")]
    assert w.log == [] and locked.alive


def test_probe_is_empty_when_every_pid_is_signalable(tmp_path):
    root = tmp_path / "mirror"
    w = _World()
    w.add(42, "python.exe", [sys.executable, _script(root, SRV)])
    assert pair_stop.probe_pair_terminable(root, psutil_module=w) == []


def test_late_unaided_exit_settles_before_the_survivor_read(tmp_path, monkeypatch):
    root = tmp_path / "mirror"
    w = _World()
    ghost = pair_stop.PairProcess(pid=50, role="server", cmdline="")
    reads = []

    def find(engine_root, *, psutil_module=None):
        reads.append(1)
        return [ghost] if len(reads) == 3 else []

    polls = []

    def gone(pid, *, psutil_module=None):
        polls.append(pid)
        return len(polls) > 1

    monkeypatch.setattr(pair_stop, "find_pair_processes", find)
    monkeypatch.setattr(pair_stop, "_pid_gone", gone)
    result = pair_stop.stop_warm_pair(root, psutil_module=w)
    assert result.ok(), result.render_survivors()
    assert len(reads) >= 4


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, secs):
        self.now += secs


class _DyingProc(_Proc):
    """Ignores terminate and kill; exits at fake-clock time `die_at`."""

    def __init__(self, world, pid, name, argv, clock, die_at):
        super().__init__(world, pid, name, argv, stubborn=True)
        self.clock = clock
        self.die_at = die_at

    @property
    def alive(self):
        return self.clock.now < self.die_at

    @alive.setter
    def alive(self, _value):
        pass


def _dying_world(root, clock, die_times):
    w = _World()
    for i, die_at in enumerate(die_times):
        w.procs.append(_DyingProc(w, 60 + i, "python.exe", [sys.executable, _script(root, SRV)], clock, die_at))
    return w


def test_staggered_slow_exit_is_waited_out_while_progress_continues(tmp_path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(pair_stop, "time", clock)
    w = _dying_world(tmp_path / "mirror", clock, [0.15, 0.19, 0.23])
    result = pair_stop.stop_warm_pair(tmp_path / "mirror", psutil_module=w)
    assert result.ok(), result.render_survivors()
    assert clock.now >= 0.23


def test_no_progress_survivor_is_reported_after_the_stall_window(tmp_path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(pair_stop, "time", clock)
    w = _dying_world(tmp_path / "mirror", clock, [0.10, 99.0])
    result = pair_stop.stop_warm_pair(tmp_path / "mirror", psutil_module=w)
    assert [p.pid for p in result.survivors] == [61]
    assert clock.now < 1.0


def test_continuous_progress_is_still_capped(tmp_path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(pair_stop, "time", clock)
    monkeypatch.setattr(pair_stop, "_TERMINATE_GRACE_SECS", 0.005)
    monkeypatch.setattr(pair_stop, "_SETTLE_MAX_SECS", 0.3)
    w = _dying_world(tmp_path / "mirror", clock, [0.22 + 0.03 * i for i in range(20)])
    result = pair_stop.stop_warm_pair(tmp_path / "mirror", psutil_module=w)
    assert not result.ok()
    assert clock.now < 0.6


def test_kill_exit_wait_uses_the_process_handle(monkeypatch):
    class _Handle(_Proc):
        killed = False

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            self.world.log.append(("wait", self.pid, timeout))
            if self.killed:
                self.alive = False

    w = _World()
    proc = _Handle(w, 70, "python.exe", [], stubborn=True)
    w.procs.append(proc)
    assert pair_stop._terminate_with_reason(70, psutil_module=w) is None
    assert ("wait", 70, pair_stop._TERMINATE_GRACE_SECS) in w.log


def _child_world(root, born_after_kill):
    w = _World()
    server = w.add(80, "python.exe", [sys.executable, _script(root, SRV)])
    child = w.add(81, "python.exe", [], stubborn=True)
    server.children = lambda recursive=True: [child]
    born = {"t": 5.0}
    child.create_time = lambda: born["t"]

    def _kill():
        w.log.append(("kill", 81))
        if born_after_kill is not None:
            born["t"] = born_after_kill

    child.kill = _kill
    return w


def test_reused_child_pid_is_not_a_survivor(tmp_path):
    root = tmp_path / "mirror"
    result = pair_stop.stop_warm_pair(root, psutil_module=_child_world(root, 9.0))
    assert result.ok(), result.render_survivors()


def test_same_birth_child_still_survives(tmp_path):
    root = tmp_path / "mirror"
    result = pair_stop.stop_warm_pair(root, psutil_module=_child_world(root, None))
    assert [p.pid for p in result.survivors] == [81]


def test_stuck_counts_original_and_refused_pids_but_not_respawns():
    from coordinator_core.warm.pair_stop import PairProcess, PairStopResult, ROLE_SERVER

    orig, respawn, refused = (PairProcess(pid=p, role=ROLE_SERVER, cmdline="") for p in (1, 2, 3))
    result = PairStopResult(
        stopped=(), survivors=(orig, respawn, refused), failures=((3, "access denied"),),
        initial_pids=frozenset({1}),
    )
    assert result.stuck() == (orig, refused)
    assert PairStopResult(stopped=(), survivors=(respawn,)).stuck() == (respawn,)


def test_stop_records_the_pids_it_set_out_to_end(monkeypatch, tmp_path):
    from coordinator_core.warm import pair_stop

    seen = [pair_stop.PairProcess(pid=11, role=pair_stop.ROLE_SERVER, cmdline="")]
    monkeypatch.setattr(pair_stop, "find_pair_processes", lambda *a, **k: list(seen) if seen else [])
    monkeypatch.setattr(pair_stop, "_terminate_pid", lambda *a, **k: seen.clear())
    monkeypatch.setattr(pair_stop, "_terminate_with_reason", lambda *a, **k: None)
    monkeypatch.setattr(pair_stop, "_pid_gone", lambda *a, **k: True)
    monkeypatch.setattr(pair_stop, "_live_descendants", lambda *a, **k: set())
    monkeypatch.setattr(pair_stop, "_pool_children", lambda *a, **k: [])
    result = pair_stop.stop_warm_pair(tmp_path, ask_server=lambda p: True, ask_front_door=lambda r: True)
    assert 11 in result.initial_pids
