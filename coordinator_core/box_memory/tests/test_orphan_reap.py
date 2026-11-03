"""orphan_reap: the dead-parent rule and the reap loop, against a fake census."""

from coordinator_core.bash_guards._heavy_admission_contract import ProcRow
from coordinator_core.box_memory import orphan_reap
from coordinator_core.diagnostics import contained_run


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid
        self.killed = False

    def kill(self):
        self.killed = True


def test_contained_run_kill_child_refuses_self_and_ancestor(monkeypatch):
    import os

    monkeypatch.setattr(orphan_reap, "protected_pids", lambda self_pid=None: frozenset({os.getpid(), 7}))
    for pid in (os.getpid(), 7):
        proc = _FakeProc(pid)
        assert contained_run._kill_child(proc) is False
        assert proc.killed is False


def test_contained_run_kill_child_kills_unrelated_pid(monkeypatch):
    monkeypatch.setattr(orphan_reap, "protected_pids", lambda self_pid=None: frozenset({1}))
    proc = _FakeProc(987654)
    assert contained_run._kill_child(proc) is True
    assert proc.killed is True


def test_contained_run_killpg_refuses_self_and_ancestor_before_any_signal(monkeypatch):
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
    def __init__(self, rows, ctimes=None):
        self.rows = rows
        self.ctimes = ctimes if ctimes is not None else {r.pid: r.ctime for r in rows}

    def alive(self, pid):
        return pid in self.ctimes

    def creation_time(self, pid):
        return self.ctimes.get(pid)

    def snapshot(self):
        return self.rows


def test_dead_parent_grep_selected_live_parent_not():
    rows = [
        ProcRow(10, 1, 100, "bash.exe"),
        ProcRow(11, 10, 200, "grep.exe"),
        ProcRow(12, 999, 200, "find"),
    ]
    got = [r.pid for r in orphan_reap.select(rows, FakePrims(rows))]
    assert got == [12]


def test_reused_pid_parent_is_dead():
    rows = [ProcRow(10, 1, 500, "bash"), ProcRow(11, 10, 200, "grep")]
    assert [r.pid for r in orphan_reap.select(rows, FakePrims(rows))] == [11]


def test_unreadable_census_and_unknown_ctime_read_alive():
    assert orphan_reap.select([], FakePrims([])) == []
    assert orphan_reap.select(None, FakePrims([])) == []
    rows = [ProcRow(10, 1, 500, "bash"), ProcRow(11, 10, 0, "grep")]
    assert orphan_reap.select(rows, FakePrims(rows)) == []


def test_non_search_orphan_is_left_alone():
    rows = [ProcRow(20, 999, 100, "cargo.exe")]
    assert orphan_reap.select(rows, FakePrims(rows)) == []


def test_reap_returns_pids_and_skips_reused_child():
    rows = [ProcRow(12, 999, 200, "grep"), ProcRow(13, 998, 300, "find")]
    prims = FakePrims(rows, ctimes={12: 200, 13: 999})
    killed = []
    assert orphan_reap.reap(prims, terminate=killed.append, self_pid=-1) == [12]
    assert killed == [12]


def test_reap_never_kills_self_and_survives_terminate_failure():
    rows = [ProcRow(12, 999, 200, "grep"), ProcRow(13, 998, 300, "grep")]

    def boom(pid):
        if pid == 13:
            raise PermissionError

    assert orphan_reap.reap(FakePrims(rows), terminate=boom, self_pid=12) == []


def test_reap_with_unreadable_snapshot_is_empty():
    class Dead(FakePrims):
        def snapshot(self):
            return None

    assert orphan_reap.reap(Dead([]), terminate=lambda p: None) == []


def test_parent_missing_from_census_but_alive_is_not_orphaned():
    rows = [ProcRow(12, 999, 200, "grep")]
    prims = FakePrims(rows, ctimes={12: 200, 999: 50})
    assert orphan_reap.select(rows, prims) == []
    assert orphan_reap.reap(prims, terminate=lambda pid: (_ for _ in ()).throw(AssertionError), self_pid=-1) == []


def test_protected_pids_include_self_and_ancestors():
    import os

    import psutil

    got = orphan_reap.protected_pids()
    assert os.getpid() in got
    assert all(p.pid in got for p in psutil.Process().parents())


def test_terminate_refuses_self_and_ancestor(monkeypatch):
    sent = []
    monkeypatch.setattr(orphan_reap.os, "kill", lambda pid, sig: sent.append(pid))
    monkeypatch.setattr(orphan_reap, "protected_pids", lambda self_pid=None: frozenset({5, 6}))
    orphan_reap._terminate(5)
    orphan_reap._terminate(6)
    assert sent == []
    orphan_reap._terminate(7)
    assert sent == [7]


def test_reap_skips_ancestor_of_self(monkeypatch):
    monkeypatch.setattr(orphan_reap, "protected_pids", lambda self_pid=None: frozenset({12}))
    rows = [ProcRow(12, 999, 200, "grep"), ProcRow(13, 998, 300, "grep")]
    killed = []
    assert orphan_reap.reap(FakePrims(rows), terminate=killed.append) == [13]
    assert killed == [13]
