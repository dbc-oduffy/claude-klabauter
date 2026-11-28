"""box_load: streak alarm, load5 degradation, ps parsing, ppid attribution."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from coordinator_core.group_em import box_load
from pathlib import Path
import pytest


def test_fires_once_after_n_ticks_and_rearms_below() -> None:
    state, fires = None, []
    for load in (9, 9, 9, 9, 9, 1, 9, 9, 9):
        state, fire = box_load.next_state(state, load, 4, n=3)
        fires.append(fire)
    assert fires == [False, False, True, False, False, False, False, False, True]


def test_equal_to_cores_does_not_count() -> None:
    state, fire = box_load.next_state({"count": 2}, 4.0, 4, n=3)
    assert (state, fire) == ({"count": 0}, False)


def test_none_load5_resets_without_firing() -> None:
    assert box_load.next_state({"count": 2}, None, 4) == ({"count": 0}, False)


def test_malformed_prev_is_initial() -> None:
    for bad in (None, "x", [], {"count": "2"}, {"count": True}, {"count": -1}, {}):
        state, fire = box_load.next_state(bad, 9, 4, n=3)
        assert state == {"count": 1} and fire is False


def test_read_load5_degrades_without_getloadavg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(box_load.os, "getloadavg", raising=False)
    assert box_load.read_load5() is None


def test_read_load5_reads_middle_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(box_load.os, "getloadavg", lambda: (1.0, 2.5, 3.0), raising=False)
    assert box_load.read_load5() == 2.5


def test_core_count_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(box_load.os, "cpu_count", lambda: None)
    assert box_load.core_count() == 1


def _ps(stdout, returncode: int = 0):
    calls = []

    def run(cmd, **kw) -> SimpleNamespace:
        calls.append((cmd, kw))
        return SimpleNamespace(stdout=stdout, returncode=returncode)

    run.calls = calls
    return run


def test_top_processes_parses_sorts_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(box_load, "_IS_WINDOWS", False)
    out = (
        "  10     1  1.5 /usr/bin/a\n"
        "  11    10 40.0 /Applications/My App/bin x\n"
        "garbage\n"
        "  12    10  x.y bad\n"
        "  13    11 20.0 c\n"
    )
    run = _ps(out)
    rows = box_load.top_processes(2, run)
    assert [r["pid"] for r in rows] == [11, 13]
    assert rows[0]["comm"] == "/Applications/My App/bin x"
    assert len(run.calls) == 1 and run.calls[0][1]["timeout"] == 1


def test_top_processes_failures_return_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(box_load, "_IS_WINDOWS", False)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("ps", 1)

    assert box_load.top_processes(8, boom) == []
    assert box_load.top_processes(8, _ps("", returncode=1)) == []
    monkeypatch.setattr(box_load, "_IS_WINDOWS", True)
    assert box_load.top_processes(8, _ps("1 0 1.0 x")) == []


def test_attribute_walks_ppid_to_session_and_maps_repo(tmp_path: Path) -> None:
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "pkg"
    sub.mkdir()
    sessions = {100: {"sessionId": "abcdef1234567", "cwd": str(sub)}}
    ppid_map = {300: 200, 200: 100, 100: 1}
    procs = [{"pid": 300, "ppid": 200, "pcpu": 50.0, "comm": "node"}]
    [row] = box_load.attribute(procs, ppid_map, sessions)
    assert (row["session"], row["repo"]) == ("abcdef12", "myrepo")


def test_attribute_unattributed_kept_and_cycle_safe() -> None:
    procs = [
        {"pid": 5, "ppid": 6, "pcpu": 1.0, "comm": "a"},
        {"pid": 7, "ppid": 1, "pcpu": 1.0, "comm": "b"},
    ]
    rows = box_load.attribute(procs, {5: 6, 6: 5, 7: 1}, {})
    assert len(rows) == 2
    assert all((r["session"], r["repo"]) == ("?", "?") for r in rows)


def test_alarm_line_shape() -> None:
    rows = [
        {"comm": "node", "pcpu": 80.0, "session": "abcdef12", "repo": "r"},
        {"comm": "x", "pcpu": 5.0, "session": "?", "repo": "?"},
    ]
    line = box_load.alarm_line(9.0, 4, 3, rows)
    assert line == "load-alarm: load5=9.00 cores=4 for 3 ticks; top: node(80.0%) abcdef12@r, x(5.0%) ?@?"


def test_cpu_busy_pct_windows_delta_and_first_sample() -> None:
    reads = iter([(100, 1000, 500), (150, 1200, 600)])
    rd = lambda: next(reads)  # noqa: E731
    pct, st = box_load.cpu_busy_pct(None, platform="windows", read_windows=rd)
    assert pct is None and st == {"idle": 100, "total": 1500}
    pct, st = box_load.cpu_busy_pct(st, platform="windows", read_windows=rd)
    assert pct == round(100 * (300 - 50) / 300, 1)


def test_cpu_busy_pct_linux_delta_and_failures() -> None:
    reads = iter([(10, 100), (20, 200)])
    rd = lambda: next(reads)  # noqa: E731
    _, st = box_load.cpu_busy_pct(None, platform="linux", read_linux=rd)
    pct, _ = box_load.cpu_busy_pct(st, platform="linux", read_linux=rd)
    assert pct == 90.0
    assert box_load.cpu_busy_pct({}, platform="linux", read_linux=lambda: None) == (None, {})

    def boom():
        raise OSError

    assert box_load.cpu_busy_pct({}, platform="windows", read_windows=boom) == (None, {})
    same = {"idle": 20, "total": 200}
    assert box_load.cpu_busy_pct(same, platform="linux", read_linux=lambda: (20, 200))[0] is None


def test_cpu_busy_pct_macos_is_load_over_cores() -> None:
    pct, st = box_load.cpu_busy_pct(
        None, platform="darwin", load5=lambda: 4.0, cores=lambda: 8
    )
    assert (pct, st) == (50.0, {})
    assert box_load.cpu_busy_pct(None, platform="darwin", load5=lambda: None)[0] is None


def test_cpu_busy_pct_real_host_does_not_raise_and_spawns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_spawn(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    _, st = box_load.cpu_busy_pct(None)
    pct, _ = box_load.cpu_busy_pct(st)
    assert pct is None or 0.0 <= pct <= 100.0


class _Host:
    def __init__(self, procs, cpu):
        self._procs, self.cpu = procs, cpu

    def snapshot(self):
        return self._procs

    def cpu_time_s(self, pid):
        return self.cpu.get(pid)


def test_windows_top_processes_deltas_reuse_and_bound() -> None:
    from coordinator_core.bash_guards._heavy_admission_contract import ProcRow

    procs = [ProcRow(0, 0, 0, "idle"), ProcRow(10, 1, 7, "a.exe"), ProcRow(11, 10, 8, "b.exe"),
             ProcRow(12, 10, 9, "c.exe")]
    host = _Host(procs, {10: 1.0, 11: 1.0, 12: 1.0})
    t = iter([100.0, 102.0])
    rows, st = box_load.windows_top_processes(None, 8, host=host, clock=lambda: next(t))
    assert rows == []
    host.cpu = {10: 1.5, 11: 3.0, 12: 1.0}
    host._procs = [ProcRow(10, 1, 7, "a.exe"), ProcRow(11, 10, 8, "b.exe"), ProcRow(12, 10, 99, "c.exe")]
    rows, _ = box_load.windows_top_processes(st, 2, host=host, clock=lambda: next(t))
    assert rows == [
        {"pid": 11, "ppid": 10, "pcpu": 100.0, "comm": "b.exe"},
        {"pid": 10, "ppid": 1, "pcpu": 25.0, "comm": "a.exe"},
    ]


def test_windows_top_processes_unreadable_snapshot_is_empty() -> None:
    host = _Host(None, {})
    assert box_load.windows_top_processes(None, host=host)[0] == []


def test_windows_top_processes_real_host_rows_feed_attribute() -> None:
    _, st = box_load.windows_top_processes(None, 5)
    rows, _ = box_load.windows_top_processes(st, 5)
    assert isinstance(rows, list) and all(set(r) == {"pid", "ppid", "pcpu", "comm"} for r in rows)


def test_read_sessions_and_top_attributed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "a.json").write_text(
        '{"pid": 10, "sessionId": "sess1234xyz", "cwd": "' + str(tmp_path).replace("\\", "/") + '"}',
        encoding="utf-8",
    )
    (tmp_path / "sessions" / "bad.json").write_text("{", encoding="utf-8")
    sessions = box_load.read_sessions(tmp_path)
    assert sessions[10]["sessionId"] == "sess1234xyz"
    monkeypatch.setattr(box_load, "_IS_WINDOWS", False)
    rows = box_load.top_attributed(
        8, _ps("  10 1 1.0 claude\n  20 10 9.0 node\n"), sessions=sessions
    )
    assert rows[0]["pid"] == 20 and rows[0]["session"] == "sess1234"
    assert box_load.read_sessions(tmp_path / "missing") == {}
