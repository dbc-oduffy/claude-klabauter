"""spawn_orphans.reap: stops only python spawn_main workers naming a dead parent, through a fake psutil."""

from __future__ import annotations

import sys
import time

import pytest

from coordinator_core.ipc import CallerFacingValidationError
from coordinator_core.ops import spawn_orphan_reap as reap_mod


class _NoSuchProcess(Exception):
    pass


class _AccessDenied(Exception):
    pass


class _Proc:
    def __init__(self, world, pid, name, argv, born, stubborn=False, denied=False):
        self.world, self.pid, self._argv, self._born = world, pid, argv, born
        self.info = {"pid": pid, "name": name}
        self.stubborn, self.denied, self.alive = stubborn, denied, True

    def cmdline(self):
        return self._argv

    def create_time(self):
        return self._born

    def terminate(self):
        if self.denied:
            raise _AccessDenied()
        self.world.log.append(("terminate", self.pid))
        if not self.stubborn:
            self.alive = False

    def kill(self):
        self.world.log.append(("kill", self.pid))
        if self.stubborn and not self.denied:
            self.alive = False


class _World:
    NoSuchProcess = _NoSuchProcess
    AccessDenied = _AccessDenied

    def __init__(self):
        self.procs, self.log, self.iter_calls = {}, [], 0

    def add(self, pid, name, argv, born=1000.0, **kw):
        self.procs[pid] = _Proc(self, pid, name, argv, born, **kw)
        return self.procs[pid]

    def pid_exists(self, pid):
        return pid in self.procs and self.procs[pid].alive

    def Process(self, pid):
        if not self.pid_exists(pid):
            raise _NoSuchProcess()
        return self.procs[pid]

    def process_iter(self, attrs=None):
        self.iter_calls += 1
        return [p for p in list(self.procs.values()) if p.alive]

    def wait_procs(self, procs, timeout=None):
        procs = list(procs)
        return [p for p in procs if not p.alive], [p for p in procs if p.alive]


def _spawn_argv(parent):
    return [
        sys.executable, "-c",
        f"from multiprocessing.spawn import spawn_main; spawn_main(parent_pid={parent}, pipe_handle=9)",
        "--multiprocessing-fork",
    ]


def _unix_spawn_argv(parent):
    return [
        sys.executable, "-c",
        f"from multiprocessing.spawn import spawn_main; spawn_main(tracker_fd=5, pipe_handle=9, parent_pid={parent})",
        "--multiprocessing-fork",
    ]


def test_dead_parent_workers_are_stopped_and_reported():
    w = _World()
    kids = [w.add(pid, "pythonw.exe", _spawn_argv(7396)) for pid in (91, 92)]
    kids.append(w.add(93, "Python3", _unix_spawn_argv(7396)))
    res = reap_mod.reap({"parent_pid": 7396}, psutil_module=w)
    assert res["ok"] and res["stopped"] == [91, 92, 93] and res["failed"] == []
    assert not any(k.alive for k in kids)
    assert w.iter_calls == 1


def test_live_parent_refuses_and_stops_nothing():
    w = _World()
    w.add(7396, "python.exe", [sys.executable, "band.py"], born=100.0)
    kid = w.add(91, "pythonw.exe", _spawn_argv(7396), born=200.0)
    res = reap_mod.reap({"parent_pid": 7396}, psutil_module=w)
    assert res == {"ok": False, "refused": "parent_alive", "parent_pid": 7396, "stopped": []}
    assert kid.alive and w.log == []


def test_live_parent_with_no_workers_still_refuses():
    w = _World()
    w.add(7396, "python.exe", ["band.py"])
    res = reap_mod.reap({"parent_pid": 7396}, psutil_module=w)
    assert res["ok"] is False and res["refused"] == "parent_alive"


def test_unreadable_parent_birth_refuses_rather_than_reaping():
    w = _World()
    parent = w.add(7396, "python.exe", ["band.py"])
    kid = w.add(91, "pythonw.exe", _spawn_argv(7396))

    def _boom():
        raise RuntimeError("unreadable")

    parent.create_time = _boom
    res = reap_mod.reap({"parent_pid": 7396}, psutil_module=w)
    assert res["ok"] is False and res["refused"] == "parent_alive" and kid.alive


def test_reused_parent_pid_does_not_shield_older_workers():
    w = _World()
    w.add(7396, "notepad.exe", ["notepad.exe"], born=500.0)
    kid = w.add(91, "pythonw.exe", _spawn_argv(7396), born=100.0)
    res = reap_mod.reap({"parent_pid": 7396}, psutil_module=w)
    assert res["ok"] and res["stopped"] == [91] and not kid.alive


def test_reused_pid_with_one_younger_worker_refuses_the_whole_call():
    w = _World()
    w.add(7396, "notepad.exe", ["notepad.exe"], born=500.0)
    old = w.add(91, "pythonw.exe", _spawn_argv(7396), born=100.0)
    young = w.add(92, "pythonw.exe", _spawn_argv(7396), born=900.0)
    res = reap_mod.reap({"parent_pid": 7396}, psutil_module=w)
    assert res["ok"] is False and old.alive and young.alive


def test_other_parent_prefix_pid_and_non_python_are_never_stopped():
    w = _World()
    target = w.add(91, "pythonw.exe", _spawn_argv(7396))
    other = w.add(92, "pythonw.exe", _spawn_argv(400))
    prefix = w.add(93, "pythonw.exe", _spawn_argv(73960))
    shorter = w.add(94, "pythonw.exe", _spawn_argv(739))
    non_py = w.add(95, "node.exe", _spawn_argv(7396))
    no_marker = w.add(96, "python.exe", [sys.executable, "-c", "x=1", "parent_pid=7396"])
    res = reap_mod.reap({"parent_pid": 7396}, psutil_module=w)
    assert res["stopped"] == [91] and not target.alive
    assert all(p.alive for p in (other, prefix, shorter, non_py, no_marker))


def test_stubborn_worker_is_killed_and_unsignalable_one_is_reported():
    w = _World()
    stubborn = w.add(91, "pythonw.exe", _spawn_argv(7396), stubborn=True)
    denied = w.add(92, "pythonw.exe", _spawn_argv(7396), denied=True)
    res = reap_mod.reap({"parent_pid": 7396}, psutil_module=w)
    assert ("kill", 91) in w.log and not stubborn.alive
    assert res["stopped"] == [91]
    assert res["failed"] == [{"pid": 92, "reason": "access denied"}] and denied.alive


def test_dry_run_matches_and_signals_nothing():
    w = _World()
    kid = w.add(91, "pythonw.exe", _spawn_argv(7396))
    res = reap_mod.reap({"parent_pid": 7396, "dry_run": True}, psutil_module=w)
    assert [m["pid"] for m in res["matched"]] == [91] and res["stopped"] == []
    assert kid.alive and w.log == []


def test_age_floor_spares_young_workers():
    w = _World()
    old = w.add(91, "pythonw.exe", _spawn_argv(7396), born=time.time() - 3600)
    new = w.add(92, "pythonw.exe", _spawn_argv(7396), born=time.time() - 5)
    res = reap_mod.reap({"parent_pid": 7396, "min_age_s": 1800}, psutil_module=w)
    assert res["stopped"] == [91] and res["young"] == [92]
    assert not old.alive and new.alive


@pytest.mark.parametrize(
    "params",
    [{}, {"parent_pid": 0}, {"parent_pid": True}, {"parent_pid": "7"}, {"parent_pid": 5, "x": 1},
     {"parent_pid": 5, "min_age_s": -1}, {"parent_pid": 5, "dry_run": "yes"}],
)
def test_bad_params_are_refused(params):
    with pytest.raises(CallerFacingValidationError):
        reap_mod.reap(params, psutil_module=_World())


def test_op_is_registered_with_a_strict_schema():
    from coordinator_core import ipc

    assert ipc._REGISTRY[reap_mod.OP_NAME] is reap_mod.reap
    assert ipc._PARAMS_SCHEMA[reap_mod.OP_NAME]["additionalProperties"] is False


def test_real_psutil_pass_finds_nothing_for_an_unused_dead_pid():
    import psutil

    dead = 2**22 + 12345
    assert not psutil.pid_exists(dead)
    start = time.process_time()
    res = reap_mod.reap({"parent_pid": dead, "dry_run": True})
    assert res["ok"] and res["matched"] == []
    assert time.process_time() - start < 0.5
