"""Tests for coordinator_core.group_em.job_ledger (pure classifier)."""

from __future__ import annotations

from coordinator_core.group_em import job_ledger as jl

NOW = 10_000.0


def _proc(pid, ppid=1, started_at=500.0, cpu_pct=90.0, **kw):
    return {"pid": pid, "ppid": ppid, "started_at": started_at, "cmdline": f"cmd{pid}", "cpu_pct": cpu_pct,
            "rss_mb": 100.0, "vram_mib": None, "io_read_bps": None, "io_write_bps": None, **kw}


def _grant(pid, granted_at=NOW - 100, **kw):
    return {"run": "go-1", "pid": pid, "pid_started_at": 500.0, "granted_at": granted_at, "ttl_s": 600.0,
            "owner_sid": "sid-a", **kw}


def test_unbooked_heavy_process_is_flagged() -> None:
    rows = jl.classify([_proc(7)], [], NOW)
    assert len(rows) == 1 and rows[0]["flags"] == ["unbooked"] and rows[0]["pid"] == 7
    assert rows[0]["go_id"] is None


def test_light_unbooked_process_is_not_listed() -> None:
    assert jl.classify([_proc(7, cpu_pct=1.0)], [], NOW) == []


def test_booked_in_window_in_rate_has_no_flags() -> None:
    rows = jl.classify([_proc(7)], [_grant(7, expected_rate=0.5, progress=100.0)], NOW)
    assert rows[0]["flags"] == [] and rows[0]["go_id"] == "go-1" and rows[0]["owner_sid"] == "sid-a"
    assert rows[0]["deadline"] == NOW - 100 + 600.0 and rows[0]["rate_so_far"] == 1.0


def test_past_deadline_is_over_window_and_window_s_overrides_ttl() -> None:
    assert jl.classify([_proc(7)], [_grant(7, granted_at=NOW - 700)], NOW)[0]["flags"] == ["over_window"]
    assert jl.classify([_proc(7)], [_grant(7, granted_at=NOW - 100, window_s=50.0)], NOW)[0]["flags"] == ["over_window"]


def test_behind_rate_flagged_only_when_both_declared() -> None:
    assert jl.classify([_proc(7)], [_grant(7, expected_rate=5.0, progress=100.0)], NOW)[0]["flags"] == ["behind_rate"]
    assert jl.classify([_proc(7)], [_grant(7, expected_rate=5.0)], NOW)[0]["flags"] == []
    assert jl.classify([_proc(7)], [_grant(7, progress=1.0)], NOW)[0]["flags"] == []


def test_reused_pid_does_not_inherit_a_stale_grant() -> None:
    rows = jl.classify([_proc(7, started_at=9000.0)], [_grant(7)], NOW)
    assert sorted(r["flags"][0] for r in rows) == ["exited", "unbooked"]


def test_heavy_children_match_the_booked_ancestor() -> None:
    shell = _proc(7, cpu_pct=0.0)
    kids = [_proc(20 + i, ppid=7, started_at=600.0) for i in range(4)]
    rows = jl.classify([shell, *kids], [_grant(7)], NOW)
    assert len(rows) == 4 and all(r["flags"] == [] and r["go_id"] == "go-1" for r in rows)


def test_grandchild_matches_and_orphan_stays_unbooked() -> None:
    procs = [_proc(7, cpu_pct=0.0), _proc(8, ppid=7, cpu_pct=0.0), _proc(9, ppid=8), _proc(30, ppid=999)]
    rows = {r["pid"]: r["flags"] for r in jl.classify(procs, [_grant(7)], NOW)}
    assert rows == {9: [], 30: ["unbooked"]}


def test_booked_light_process_is_listed_through_its_own_row() -> None:
    rows = jl.classify([_proc(7, cpu_pct=0.0)], [_grant(7, granted_at=NOW - 700)], NOW)
    assert len(rows) == 1 and rows[0]["pid"] == 7 and rows[0]["flags"] == ["over_window"]


def test_grant_whose_pid_is_gone_is_exited() -> None:
    rows = jl.classify([], [_grant(7)], NOW)
    assert len(rows) == 1 and rows[0]["flags"] == ["exited"] and rows[0]["go_id"] == "go-1"


def test_ancestry_cycle_terminates() -> None:
    procs = [_proc(1, ppid=2), _proc(2, ppid=1)]
    assert [r["flags"] for r in jl.classify(procs, [], NOW)] == [["unbooked"], ["unbooked"]]


def test_is_heavy_per_axis_and_thresholds_override() -> None:
    quiet = {"pid": 1, "cpu_pct": 1.0, "rss_mb": 10.0}
    assert not jl.is_heavy(quiet)
    assert jl.is_heavy({**quiet, "rss_mb": 4096.0})
    assert jl.is_heavy({**quiet, "vram_mib": 1024.0})
    assert jl.is_heavy({**quiet, "io_read_bps": 15e6, "io_write_bps": 15e6})
    assert not jl.is_heavy({**quiet, "cpu_pct": None, "io_read_bps": None})
    assert jl.is_heavy(quiet, {"cpu_pct": 0.5})
    t = jl.resolve_thresholds({"group_em.job_ledger.heavy_cpu_pct": 5, "group_em.job_ledger.heavy_rss_mb": "x"})
    assert t["cpu_pct"] == 5.0 and t["rss_mb"] == jl.DEFAULT_THRESHOLDS["rss_mb"]
