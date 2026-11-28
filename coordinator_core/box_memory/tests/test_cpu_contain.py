"""cpu_contain: orphan terminated, live-parented saturator lowered, protected and self skipped."""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from coordinator_core.box_memory import cpu_contain


class _Prims:
    def __init__(self, ctimes=None, cpu=None):
        self.ctimes = ctimes or {}
        self.cpu = cpu or {}

    def creation_time(self, pid):
        return self.ctimes.get(pid)

    def cpu_time_s(self, pid):
        seq = self.cpu.get(pid)
        return seq.pop(0) if seq else None


def _row(pid, name="burner.exe", ctime=100):
    return SimpleNamespace(pid=pid, ppid=1, ctime=ctime, name=name)


def _run(rows, prims, orphans=(), protected=(), saturated=True, **kw):
    killed, lowered = [], []
    actions = cpu_contain.contain(
        rows, prims, lambda r: r.pid in orphans, protected, saturated,
        terminate=killed.append, lower=lowered.append, self_pid=os.getpid(), **kw,
    )
    return actions, killed, lowered


def test_not_saturated_touches_nothing():
    prims = _Prims({10: 100})
    actions, killed, lowered = _run([_row(10)], prims, orphans={10}, saturated=False)
    assert actions == [] and killed == [] and lowered == []


def test_orphan_terminated_and_live_parented_lowered():
    prims = _Prims({10: 100, 11: 100})
    actions, killed, lowered = _run([_row(10), _row(11)], prims, orphans={10})
    assert killed == [10] and lowered == [11]
    assert actions == [
        {"pid": 10, "name": "burner.exe", "action": "terminate", "ok": True},
        {"pid": 11, "name": "burner.exe", "action": "lower_priority", "ok": True},
    ]


def test_protected_name_with_or_without_exe_is_skipped_but_unprotected_acts():
    prims = _Prims({10: 100, 11: 100, 12: 100})
    rows = [_row(10, "Claude.EXE"), _row(11, "codex"), _row(12, "burner.exe")]
    actions, killed, lowered = _run(rows, prims, protected=("claude", "codex.exe"))
    assert [a["pid"] for a in actions] == [12]
    assert killed == [] and lowered == [12]


def test_self_and_ancestors_are_skipped():
    me = os.getpid()
    parent = os.getppid()
    prims = _Prims({me: 100, parent: 100, 99999: 100})
    actions, killed, lowered = _run([_row(me), _row(parent), _row(99999)], prims, orphans={me, parent})
    assert [a["pid"] for a in actions] == [99999]


def test_reused_or_gone_pid_is_skipped():
    prims = _Prims({10: 555})  # 10 was reused; 11 is gone
    actions, killed, lowered = _run([_row(10), _row(11)], prims, orphans={10, 11})
    assert actions == [] and killed == []


def test_refused_action_reports_not_ok_and_continues():
    prims = _Prims({10: 100, 11: 100})

    def boom(pid):
        raise PermissionError("denied")

    actions = cpu_contain.contain(
        [_row(10), _row(11)], prims, lambda r: r.pid == 10, (), True,
        terminate=boom, lower=lambda pid: None, self_pid=os.getpid(),
    )
    assert [(a["pid"], a["ok"]) for a in actions] == [(10, False), (11, True)]


def test_raising_orphan_predicate_reads_as_live_parent():
    prims = _Prims({10: 100})

    def bad(row):
        raise RuntimeError("x")

    lowered = []
    actions = cpu_contain.contain([_row(10)], prims, bad, (), True, lower=lowered.append, self_pid=os.getpid())
    assert actions[0]["action"] == "lower_priority" and lowered == [10]


def test_dry_run_acts_on_nothing():
    prims = _Prims({10: 100, 11: 100})
    actions, killed, lowered = _run([_row(10), _row(11)], prims, orphans={10}, dry_run=True)
    assert killed == [] and lowered == []
    assert [a["action"] for a in actions] == ["would_terminate", "would_lower_priority"]


def test_dict_rows_are_accepted():
    prims = _Prims({10: 100})
    lowered = []
    actions = cpu_contain.contain(
        [{"pid": 10, "name": "burner", "ctime": 100}], prims, lambda r: False, (), True,
        lower=lowered.append, self_pid=os.getpid(),
    )
    assert lowered == [10] and actions[0]["name"] == "burner"


def test_cpu_shares_is_percent_of_one_core_and_omits_unreadable():
    prims = _Prims(cpu={10: [1.0, 1.5], 11: [None, 2.0], 12: [3.0, 2.0]})
    ticks = iter([0.0, 0.5])
    shares = cpu_contain.cpu_shares(prims, [_row(10), _row(11), _row(12)], 0.5,
                                    sleep=lambda s: None, clock=lambda: next(ticks))
    assert shares == {10: pytest.approx(100.0)}


def test_lower_priority_really_lowers_a_child():
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
    try:
        cpu_contain.lower_priority(child.pid)
        if sys.platform == "win32":
            import ctypes

            k = ctypes.WinDLL("kernel32")
            k.OpenProcess.restype = ctypes.c_void_p
            k.GetPriorityClass.argtypes = [ctypes.c_void_p]
            handle = k.OpenProcess(0x1000, False, child.pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            try:
                assert k.GetPriorityClass(handle) == 0x4000
            finally:
                k.CloseHandle(ctypes.c_void_p(handle))
        else:
            assert os.getpriority(os.PRIO_PROCESS, child.pid) >= 10
    finally:
        child.kill()
        child.wait()


def test_lower_priority_never_touches_self():
    cpu_contain.lower_priority(os.getpid())  # protected: no-op, no raise
