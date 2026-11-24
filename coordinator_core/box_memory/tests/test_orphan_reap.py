"""orphan_reap: the dead-parent rule, the reap loop and the stdin-python flag class, against a fake census."""

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


class CpuPrims(FakePrims):
    def __init__(self, rows, cpu, cmdlines) -> None:
        super().__init__(rows)
        self.cpu = cpu
        self.cmdlines = cmdlines
        self.cmd_reads = []

    def cpu_time_s(self, pid):
        return self.cpu.get(pid)

    def command_line(self, pid):
        self.cmd_reads.append(pid)
        return self.cmdlines.get(pid)


def _stdin_census():
    from coordinator_core.box_memory import pressure

    old = _NOW - 7200 * _UNIT
    young = _NOW - 60 * _UNIT
    rows = [
        ProcRow(1, 0, old - 10, "claude.exe"),
        ProcRow(2, 1, old - 5, "bash.exe"),
        ProcRow(10, 2, old, "python.exe"),       # aged, hot, stdin, claude reachable
        ProcRow(11, 998, old, "python.exe"),     # aged, hot, stdin, orphaned chain
        ProcRow(12, 2, young, "python.exe"),     # live child: young
        ProcRow(13, 2, old, "python.exe"),       # aged but under the CPU gate
        ProcRow(14, 2, old, "python.exe"),       # aged, hot, script file
        ProcRow(15, 2, old, "python.exe"),       # cpu unreadable
        ProcRow(16, 2, old, "node.exe"),         # not python
    ]
    hot = pressure.MIN_ORPHAN_CPU_S
    cpu = {10: hot, 11: hot + 1, 12: hot * 9, 13: hot - 1, 14: hot, 16: hot}
    cmd = {pid: "python -I -" for pid in (10, 11, 12, 13, 15, 16)}
    cmd[14] = "python job.py"
    return rows, CpuPrims(rows, cpu, cmd)


def test_stdin_python_aged_hot_is_flagged_live_child_and_others_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    _units(monkeypatch)
    rows, prims = _stdin_census()
    assert [r.pid for r in orphan_reap.select_stdin_python(rows, prims, now=_NOW)] == [10, 11]
    hits = orphan_reap.reap_stdin_python(rows, prims, now=_NOW)
    assert [(h["pid"], h["action"]) for h in hits] == [(10, "flagged"), (11, "flagged")]
    assert hits[0]["session"] == {"claude_pid": 1, "claude_ctime": rows[0].ctime}
    assert hits[1]["session"] is None
    assert hits[0]["cmdline"] == "python -I -" and hits[0]["cpu_s"] >= 600


def test_stdin_python_reads_command_line_only_past_age_and_cpu_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    _units(monkeypatch)
    rows, prims = _stdin_census()
    orphan_reap.select_stdin_python(rows, prims, now=_NOW)
    assert sorted(prims.cmd_reads) == [10, 11, 14]


def test_stdin_python_empty_or_unreadable_census_is_empty() -> None:
    prims = CpuPrims([], {}, {})
    assert orphan_reap.select_stdin_python(None, prims) == []
    assert orphan_reap.reap_stdin_python([], prims) == []
