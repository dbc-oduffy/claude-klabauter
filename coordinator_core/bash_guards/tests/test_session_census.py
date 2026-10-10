"""Synthetic-tree tests for _session_census."""

from __future__ import annotations

from coordinator_core.bash_guards._heavy_admission_contract import ProcRow
from coordinator_core.bash_guards._session_census import resolve_anchor, session_census


class _Prims:
    def __init__(self, rows):
        self._rows = rows

    def snapshot(self):
        return self._rows

    def alive(self, pid, ctime):
        return True

    def creation_time(self, pid):
        return None

    def parent(self, pid):
        return None

    def image_name(self, pid):
        return None


def R(pid, ppid, ctime, name):
    return ProcRow(pid, ppid, ctime, name)


def test_chain_through_wrappers_finds_claude():
    rows = [R(1, 0, 10, "claude.exe"), R(2, 1, 20, "bash.exe"), R(3, 2, 30, "cmd.exe"),
            R(4, 3, 40, "pwsh.exe"), R(5, 4, 50, "python.exe")]
    a = resolve_anchor({"pid": 5}, _Prims(rows))
    assert a is not None and a.pid == 1


def test_nested_claude_is_nearest_anchor():
    rows = [R(1, 0, 10, "claude.exe"), R(2, 1, 20, "bash"), R(3, 2, 30, "claude"),
            R(4, 3, 40, "python")]
    assert resolve_anchor({"pid": 4}, _Prims(rows)).pid == 3


def test_pid_reuse_hop_fails_closed():
    rows = [R(1, 0, 10, "claude.exe"), R(2, 1, 99, "bash"), R(3, 2, 30, "python")]
    assert resolve_anchor({"pid": 3}, _Prims(rows)) is None


def test_unresolvable_anchor_inputs():
    rows = [R(1, 0, 10, "systemd"), R(2, 1, 20, "python")]
    p = _Prims(rows)
    assert resolve_anchor({"pid": 2}, p) is None
    assert resolve_anchor({}, p) is None
    assert resolve_anchor({"pid": "x"}, p) is None
    assert resolve_anchor({"pid": 77}, p) is None
    assert resolve_anchor({"pid": 2}, _Prims(None)) is None


def test_census_counts_heavy_and_shells_and_stops_at_nested_claude():
    rows = [R(1, 0, 10, "claude.exe"), R(2, 1, 20, "bash"), R(3, 2, 30, "node.exe"),
            R(4, 1, 25, "pwsh"), R(5, 1, 26, "claude.exe"), R(6, 5, 40, "node.exe"),
            R(7, 1, 27, "conhost.exe")]
    p = _Prims(rows)
    c = session_census(rows[0], p)
    assert [r.pid for r in c.heavy] == [3]
    assert sorted(r.pid for r in c.shells) == [2, 4]


def test_orphan_attributed_by_image_and_ctime():
    rows = [R(1, 0, 10, "claude.exe"), R(8, 999, 50, "vitest"), R(9, 999, 5, "node"),
            R(10, 999, 60, "notepad")]
    c = session_census(rows[0], _Prims(rows))
    assert [r.pid for r in c.heavy] == [8]


def test_reused_pid_child_not_descended():
    rows = [R(1, 0, 10, "claude.exe"), R(2, 1, 20, "bash"), R(3, 2, 15, "node")]
    c = session_census(rows[0], _Prims(rows))
    assert c.heavy == ()


def test_exclude_pids_drops_own_wrapper():
    rows = [R(1, 0, 10, "claude.exe"), R(2, 1, 20, "bash"), R(3, 1, 21, "bash")]
    c = session_census(rows[0], _Prims(rows), exclude_pids={2})
    assert [r.pid for r in c.shells] == [3]


def test_unreadable_snapshot_returns_none():
    assert session_census(R(1, 0, 10, "claude"), _Prims(None)) is None
