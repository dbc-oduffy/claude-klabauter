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


def test_unanswered_ask_is_not_asked_and_terminates_directly(tmp_path, monkeypatch):
    root = tmp_path / "mirror"
    w = _World()
    srv = w.add(40, "python.exe", [sys.executable, _script(root, SRV)])
    monkeypatch.setattr(
        breadcrumb, "read_breadcrumb", lambda r=None: {"pid": 40, "pipe": "p", "stable_pid_start_epoch": 1}
    )
    monkeypatch.setattr(pair_stop, "stable_pid_alive", lambda pid, stored_start_epoch="": srv.alive)
    monkeypatch.setattr(pair_stop, "_ASK_READ_DEADLINE_SECS", 0.05)
    monkeypatch.setattr(pair_stop, "_ASK_CONFIRM_GRACE_SECS", 30.0)
    monkeypatch.setattr(pair_stop, "_open_pipe", lambda pipe: _Pipe(None))
    started = pair_stop.time.monotonic()
    result = pair_stop.stop_warm_pair(root, psutil_module=w)
    assert pair_stop.time.monotonic() - started < 5.0
    assert ("terminate", 40) in w.log and result.ok()


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


def test_terminate_order_is_supervisor_front_door_server(tmp_path, monkeypatch):
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "rt"))
    root = tmp_path / "mirror"
    w = _World()
    w.add(30, "python.exe", [sys.executable, _script(root, SRV)])
    w.add(20, "python.exe", [sys.executable, _script(root, FD)])
    w.add(10, "python.exe", [sys.executable, _script(root, SUP)])
    result = pair_stop.stop_warm_pair(root, psutil_module=w)
    assert [pid for kind, pid in w.log if kind == "terminate"] == [10, 20, 30]
    assert result.ok() and {p.role for p in result.stopped} == {"supervisor", "front_door", "server"}


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


def test_access_denied_process_is_skipped(tmp_path):
    w = _World()
    bad = w.add(9, "python.exe", [])

    def boom():
        raise _AccessDenied()

    bad.cmdline = boom
    assert pair_stop.find_pair_processes(tmp_path, psutil_module=w) == []
