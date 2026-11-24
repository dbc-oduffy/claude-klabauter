"""Tests for group_em/fleet_balance: idle-box dwell decision over fleet live snapshot rows."""

from __future__ import annotations

import pytest

from coordinator_core.group_em import fleet_balance as fb

pytestmark = pytest.mark.complete

NOW = 100_000.0


def _row(box, state="ok", cpu_pct=90.0, gpu_util=None, age=5.0, **extra):
    row = {"box": box, "state": state, "sampled_at": NOW - age, "cores": 16, "cpu_pct": cpu_pct}
    if gpu_util is not None:
        row["gpus"] = [{"index": 0, "util_pct": gpu_util}]
        row["gpu_util_pct"] = gpu_util
    row.update(extra)
    return row


def test_dwell_carry_fires_at_16_min_not_at_14() -> None:
    rows = [_row("a", cpu_pct=5.0), _row("b", cpu_pct=95.0)]
    short, d1 = fb.scan(rows, {}, {}, {"a": NOW - 14 * 60}, NOW)
    assert short == [] and d1 == {"a": NOW - 14 * 60}
    alerts, d2 = fb.scan(rows, {}, {}, {"a": NOW - 16 * 60}, NOW)
    assert alerts == [{"idle_box": "a", "idle_s": 960.0, "busy_boxes": ["b"]}]
    assert d2 == {"a": NOW - 16 * 60}


def test_first_sight_starts_the_clock_and_a_busy_box_drops_it() -> None:
    _, d = fb.scan([_row("a", cpu_pct=5.0), _row("b")], {}, {}, {}, NOW)
    assert d == {"a": NOW}
    _, d = fb.scan([_row("a", cpu_pct=70.0), _row("b")], {}, {}, {"a": NOW - 2000}, NOW)
    assert d == {}


def test_no_other_work_is_silent() -> None:
    rows = [_row("a", cpu_pct=5.0), _row("b", cpu_pct=60.0)]
    assert fb.scan(rows, {}, {}, {"a": NOW - 2000}, NOW)[0] == []


def test_wait_or_grant_row_on_another_box_is_work() -> None:
    rows = [_row("a", cpu_pct=5.0), _row("b", cpu_pct=60.0)]
    for waits, grants in (({"b": [{"run": "r"}]}, {}), ({}, {"b": [{"job_type": "x"}]})):
        alerts, _ = fb.scan(rows, waits, grants, {"a": NOW - 2000}, NOW)
        assert [a["idle_box"] for a in alerts] == ["a"]
    assert fb.scan(rows, {"a": [{"run": "r"}]}, {}, {"a": NOW - 2000}, NOW)[0] == []


def test_half_cores_rule_boundary() -> None:
    assert fb.is_idle(_row("a", cpu_pct=50.0), NOW)
    assert not fb.is_idle(_row("a", cpu_pct=50.1), NOW)
    assert fb.is_idle(_row("a", cpu_pct=None, load5=8.0), NOW)
    assert not fb.is_idle(_row("a", cpu_pct=None, load5=9.0), NOW)
    assert not fb.is_idle(_row("a", cpu_pct=None), NOW)


def test_gpu_rule_boundary_overrides_busy_cpu() -> None:
    assert fb.is_idle(_row("a", cpu_pct=95.0, gpu_util=9.9), NOW)
    assert not fb.is_idle(_row("a", cpu_pct=95.0, gpu_util=10.0), NOW)


def test_load_80_is_work_and_79_is_not() -> None:
    assert fb.has_work(_row("b", cpu_pct=80.0), None, None, NOW)
    assert not fb.has_work(_row("b", cpu_pct=79.9), None, None, NOW)


@pytest.mark.parametrize("state", ["stale", "unreachable", "timed_out", "error", "busy"])
def test_non_ok_row_is_never_idle_and_never_work(state) -> None:
    row = _row("a", state=state, cpu_pct=1.0, gpu_util=0.0)
    assert not fb.is_idle(row, NOW)
    assert not fb.has_work({**row, "cpu_pct": 99.0}, None, None, NOW)


def test_old_sample_is_stale() -> None:
    old = _row("a", cpu_pct=1.0, age=fb.FRESH_S + 1)
    assert not fb.is_idle(old, NOW)
    assert not fb.has_work({**old, "cpu_pct": 99.0}, None, None, NOW)
    assert not fb.is_idle({"box": "a", "state": "ok", "cpu_pct": 1.0}, NOW)
    assert fb.is_idle({"box": "a", "state": "ok", "cpu_pct": 1.0, "age_s": 3.0}, NOW)


def test_stale_idle_row_never_alerts_and_duplicate_box_counts_once() -> None:
    rows = [_row("a", state="unreachable", cpu_pct=1.0), _row("b", cpu_pct=95.0)]
    assert fb.scan(rows, {}, {}, {"a": NOW - 2000}, NOW) == ([], {})
    dup = [_row("a", cpu_pct=5.0), _row("a", cpu_pct=5.0), _row("b", cpu_pct=95.0)]
    alerts, _ = fb.scan(dup, {}, {}, {"a": NOW - 2000}, NOW)
    assert len(alerts) == 1


def test_never_raises_on_garbage() -> None:
    assert fb.scan(None, None, None, None, NOW) == ([], {})
    assert fb.scan([1, {"box": 3}, None], {"a": 5}, [], {"a": "x"}, NOW) == ([], {})
    assert fb.is_idle(None) is False and fb.has_work(None, 1, 2) is False
    assert fb.scan([_row("a", cpu_pct=5.0)], {}, {}, {"a": object()}, NOW)[0] == []
