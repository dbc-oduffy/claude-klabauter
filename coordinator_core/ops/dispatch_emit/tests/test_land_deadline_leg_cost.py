"""land_deadline.leg_costs() entries carry the worktree bytes each leg read."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git import commit
from coordinator_core.ops.dispatch_emit import land_deadline

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _read(root, rel: str, size: int) -> None:
    (root / rel).write_bytes(b"x" * size)
    assert commit.checkin_blob_sha(root, rel) is not None


def test_each_leg_reports_only_its_own_bytes(tmp_path):
    with land_deadline.scope(budget_secs=None):
        _read(tmp_path, "a.txt", 100)
        land_deadline.check("land_run.dirty_scan")
        _read(tmp_path, "b.txt", 30)
        _read(tmp_path, "c.txt", 12)
        land_deadline.check("land_run.next")
        costs = land_deadline.leg_costs()
    by_leg = {c["leg"]: c for c in costs}
    assert by_leg[land_deadline.SCOPE_LEG]["bytes_read"] == 100
    assert by_leg["land_run.dirty_scan"]["bytes_read"] == 42
    assert by_leg["land_run.next"]["bytes_read"] == 0
    assert all("bytes_read" in c and "spawns" in c for c in costs)


def test_a_halt_carries_bytes_read(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(land_deadline, "_now", lambda: clock[0])
    with land_deadline.scope(budget_secs=1.0):
        land_deadline.check("land_run.dirty_scan")
        _read(tmp_path, "a.txt", 64)
        clock[0] = 99.0
        with pytest.raises(land_deadline.LandDeadlineExceeded) as exc:
            land_deadline.check("land_run.later")
    dirty = next(e for e in exc.value.legs if e["leg"] == "land_run.dirty_scan")
    assert dirty["bytes_read"] == 64


def test_leg_costs_is_empty_outside_a_scope_and_the_counter_unbinds(tmp_path):
    assert land_deadline.leg_costs() == []
    with land_deadline.scope(budget_secs=None):
        pass
    assert commit.bytes_read_total() == 0
    (tmp_path / "z.txt").write_bytes(b"zzz")
    commit.checkin_blob_sha(tmp_path, "z.txt")
    assert commit.bytes_read_total() == 0


def test_a_nested_scope_shares_the_outer_counter(tmp_path):
    with land_deadline.scope(budget_secs=None):
        _read(tmp_path, "a.txt", 10)
        with land_deadline.scope(budget_secs=None):
            _read(tmp_path, "b.txt", 5)
        assert commit.bytes_read_total() == 15
