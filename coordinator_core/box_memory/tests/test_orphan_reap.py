"""orphan_reap: the dead-parent rule and the reap loop, against a fake census."""

import pytest

from coordinator_core.bash_guards._heavy_admission_contract import ProcRow
from coordinator_core.box_memory import orphan_reap
from coordinator_core.diagnostics import contained_run


@pytest.fixture(autouse=True)
def _far_future_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.box_memory import pressure

    monkeypatch.setattr(pressure, "now_ctime", lambda: 10**15)


class _FakeProc:
    def __init__(self, pid) -> None:
        self.pid = pid
        self.killed = False

    def kill(self) -> None:
        self.killed = True


def test_contained_run_kill_child_refuses_self_and_ancestor(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.setattr(orphan_reap, "protected_pids", lambda self_pid=None: frozenset({os.getpid(), 7}))
    for pid in (os.getpid(), 7):
        proc = _FakeProc(pid)
        assert contained_run._kill_child(proc) is False
        assert proc.killed is False


def test_contained_run_kill_child_kills_unrelated_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orphan_reap, "protected_pids", lambda self_pid=None: frozenset({1}))
    proc = _FakeProc(987654)
    assert contained_run._kill_child(proc) is True
    assert proc.killed is True


def test_contained_run_killpg_refuses_self_and_ancestor_before_any_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    import signal

    calls = []
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(contained_run.os, "killpg", lambda *a: calls.append(a), raising=False)
    monkeypatch.setattr(contained_run.os, "getpgrp", lambda: 555, raising=False)
    monkeypatch.setattr(orphan_reap, "protected_pids", lambda self_pid=None: frozenset({os.getpid(), 7}))
    for pgid in (os.getpid(), 7, 555):
        assert contained_run._killpg_guarded(pgid) is False
    assert calls == []
    assert contained_run._killpg_guarded(987654) is True
    assert len(calls) == 1


class FakePrims:
    def __init__(self, rows, ctimes=None) -> None:
        self.rows = rows
        self.ctimes = ctimes if ctimes is not None else {r.pid: r.ctime for r in rows}

    def alive(self, pid) -> bool:
        return pid in self.ctimes

    def creation_time(self, pid):
        return self.ctimes.get(pid)

    def snapshot(self):
        return self.rows


def test_dead_parent_grep_selected_live_parent_not() -> None:
    rows = [
        ProcRow(10, 1, 100, "bash.exe"),
        ProcRow(11, 10, 200, "grep.exe"),
        ProcRow(12, 999, 200, "find"),
    ]
    got = [r.pid for r in orphan_reap.select(rows, FakePrims(rows))]
    assert got == [12]


def test_reused_pid_parent_is_dead() -> None:
    rows = [ProcRow(10, 1, 500, "bash"), ProcRow(11, 10, 200, "grep")]
    assert [r.pid for r in orphan_reap.select(rows, FakePrims(rows))] == [11]


def test_unreadable_census_and_unknown_ctime_read_alive() -> None:
    assert orphan_reap.select([], FakePrims([])) == []
    assert orphan_reap.select(None, FakePrims([])) == []
    rows = [ProcRow(10, 1, 500, "bash"), ProcRow(11, 10, 0, "grep")]
    assert orphan_reap.select(rows, FakePrims(rows)) == []


def test_non_search_orphan_is_left_alone() -> None:
    rows = [ProcRow(20, 999, 100, "cargo.exe")]
    assert orphan_reap.select(rows, FakePrims(rows)) == []


def test_reap_returns_pids_and_skips_reused_child() -> None:
    rows = [ProcRow(12, 999, 200, "grep"), ProcRow(13, 998, 300, "find")]
    prims = FakePrims(rows, ctimes={12: 200, 13: 999})
    killed = []
    assert orphan_reap.reap(prims, terminate=killed.append, self_pid=-1) == [12]
    assert killed == [12]


def test_reap_never_kills_self_and_survives_terminate_failure() -> None:
    rows = [ProcRow(12, 999, 200, "grep"), ProcRow(13, 998, 300, "grep")]

    def boom(pid) -> None:
        if pid == 13:
            raise PermissionError

    assert orphan_reap.reap(FakePrims(rows), terminate=boom, self_pid=12) == []


def test_reap_with_unreadable_snapshot_is_empty() -> None:
    class Dead(FakePrims):
        def snapshot(self) -> None:
            return None

    assert orphan_reap.reap(Dead([]), terminate=lambda p: None) == []


def test_parent_missing_from_census_but_alive_is_not_orphaned() -> None:
    rows = [ProcRow(12, 999, 200, "grep")]
    prims = FakePrims(rows, ctimes={12: 200, 999: 50})
    assert orphan_reap.select(rows, prims) == []
    assert orphan_reap.reap(prims, terminate=lambda pid: (_ for _ in ()).throw(AssertionError), self_pid=-1) == []


_UNIT = 10_000_000
_NOW = 10_000 * _UNIT


def _units(monkeypatch) -> None:
    from coordinator_core.bash_guards import _host_probe

    monkeypatch.setattr(_host_probe, "ctime_units_per_second", lambda: _UNIT)


def test_young_dead_parent_search_is_neither_selected_nor_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    _units(monkeypatch)
    rows = [ProcRow(12, 999, _NOW - 60 * _UNIT, "grep")]
    prims = FakePrims(rows)
    assert orphan_reap.select(rows, prims, now=_NOW) == []
    assert orphan_reap.reap(prims, terminate=lambda pid: (_ for _ in ()).throw(AssertionError),
                            self_pid=-1, now=_NOW) == []


def test_aged_dead_parent_search_is_selected_and_reaped_at_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    _units(monkeypatch)
    edge = _NOW - 30 * 60 * _UNIT
    rows = [ProcRow(12, 999, edge, "grep"), ProcRow(13, 999, edge + 1, "grep")]
    killed = []
    assert [r.pid for r in orphan_reap.select(rows, FakePrims(rows), now=_NOW)] == [12]
    assert orphan_reap.reap(FakePrims(rows), terminate=killed.append, self_pid=-1, now=_NOW) == [12]
    assert killed == [12]


def test_aged_search_with_live_parent_or_zero_ctime_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    _units(monkeypatch)
    old = _NOW - 7200 * _UNIT
    rows = [ProcRow(10, 1, old - 1, "bash"), ProcRow(11, 10, old, "grep"), ProcRow(12, 999, 0, "grep")]
    assert orphan_reap.select(rows, FakePrims(rows), now=_NOW) == []


def test_reap_refuses_aged_row_whose_pid_was_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    _units(monkeypatch)
    rows = [ProcRow(12, 999, _NOW - 7200 * _UNIT, "grep")]
    prims = FakePrims(rows, ctimes={12: _NOW - 5 * _UNIT})
    assert orphan_reap.reap(prims, terminate=lambda pid: (_ for _ in ()).throw(AssertionError),
                            self_pid=-1, now=_NOW) == []


def test_real_live_pipeline_with_dead_parent_is_not_reaped_until_aged(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess
    import sys

    from coordinator_core.bash_guards import _host_probe
    from coordinator_core.bash_guards._host_probe import HostPrimitives

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    parent_code = (
        "import subprocess, sys;"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],"
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL);"
        "print(c.pid, flush=True)"
    )
    out = subprocess.run([sys.executable, "-c", parent_code], capture_output=True, text=True,
                         timeout=30, creationflags=flags)
    child_pid = int(out.stdout.strip())
    # The orphaned child sleeps a bounded 20 s and exits itself; the harness refuses a kill outside the test tree.
    real = next((r for r in HostPrimitives.snapshot() or () if r.pid == child_pid), None)
    assert real is not None and real.ctime
    row = ProcRow(real.pid, real.ppid, real.ctime, "grep")

    class Prims:
        @staticmethod
        def creation_time(pid):
            # The exited parent's handle can linger in this process (Windows); report it gone.
            return None if pid == real.ppid else HostPrimitives.creation_time(pid)

        @staticmethod
        def snapshot() -> list[ProcRow]:
            return [row]

    per_s = _host_probe.ctime_units_per_second()
    killed = []
    young = row.ctime + 60 * per_s
    assert orphan_reap.reap(Prims, terminate=killed.append, self_pid=-1, now=young) == []
    assert killed == []
    aged = row.ctime + 31 * 60 * per_s
    assert orphan_reap.reap(Prims, terminate=killed.append, self_pid=-1, now=aged) == [child_pid]


def test_protected_pids_include_self_and_ancestors() -> None:
    import os

    import psutil

    got = orphan_reap.protected_pids()
    assert os.getpid() in got
    assert all(p.pid in got for p in psutil.Process().parents())


def test_terminate_refuses_self_and_ancestor(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = []
    monkeypatch.setattr(orphan_reap.os, "kill", lambda pid, sig: sent.append(pid))
    monkeypatch.setattr(orphan_reap, "protected_pids", lambda self_pid=None: frozenset({5, 6}))
    orphan_reap._terminate(5)
    orphan_reap._terminate(6)
    assert sent == []
    orphan_reap._terminate(7)
    assert sent == [7]


def test_reap_skips_ancestor_of_self(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orphan_reap, "protected_pids", lambda self_pid=None: frozenset({12}))
    rows = [ProcRow(12, 999, 200, "grep"), ProcRow(13, 998, 300, "grep")]
    killed = []
    assert orphan_reap.reap(FakePrims(rows), terminate=killed.append) == [13]
    assert killed == [13]
