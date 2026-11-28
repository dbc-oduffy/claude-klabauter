"""cpu_saturation: streak, wake line, CPU-orphan predicate, thresholds."""

from __future__ import annotations

from types import SimpleNamespace

from coordinator_core.bash_guards._host_probe import ctime_units_per_second
from coordinator_core.group_em import cpu_saturation as cs

PER_S = ctime_units_per_second()
NOW = 10_000 * PER_S


def _row(pid=10, ppid=5, age_s=600, name="burn.exe"):
    return SimpleNamespace(pid=pid, ppid=ppid, ctime=NOW - age_s * PER_S, name=name)


def test_fires_once_after_n_ticks_and_rearms() -> None:
    state, fires = None, []
    for busy in (99, 99, 99, 99, 50, 99, 99, 99):
        state, fire = cs.next_state(state, busy, 90, n=3)
        fires.append(fire)
    assert fires == [False, False, True, False, False, False, False, True]


def test_at_threshold_none_and_malformed_do_not_count() -> None:
    assert cs.next_state({"count": 2}, 89.9, 90, 3) == ({"count": 0}, False)
    assert cs.next_state({"count": 2}, 90, 90, 3) == ({"count": 3}, True)  # at threshold counts
    assert cs.next_state({"count": 2}, None, 90, 3) == ({"count": 0}, False)
    assert cs.next_state({"count": 2}, "x", 90, 3) == ({"count": 0}, False)
    for bad in (None, "x", {"count": True}, {"count": -1}, {}):
        assert cs.next_state(bad, 99, 90, 3) == ({"count": 1}, False)


def test_saturation_line_names_box_and_top() -> None:
    line = cs.saturation_line("gipsy", 97.4, 3, [{"name": "ffmpeg", "pid": 7, "cpu": 380}])
    assert line == "SATURATION box=gipsy busy=97% ticks=3 top: ffmpeg[7](380%)"
    assert "top: none" in cs.saturation_line("a", None, 3, None)


def test_orphan_requires_dead_parent_age_and_cpu() -> None:
    by_pid = {}  # parent 5 absent, no primitives: death confirmed
    assert cs.is_cpu_orphan(_row(), by_pid, None, NOW, 95)
    assert not cs.is_cpu_orphan(_row(age_s=60), by_pid, None, NOW, 95)  # young
    assert not cs.is_cpu_orphan(_row(), by_pid, None, NOW, 10)  # not CPU-bound
    assert not cs.is_cpu_orphan(_row(), by_pid, None, NOW, None)  # no sample
    live = {5: SimpleNamespace(pid=5, ppid=1, ctime=NOW - 9000 * PER_S, name="shell")}
    assert not cs.is_cpu_orphan(_row(), live, None, NOW, 95)  # live parent
    assert not cs.is_cpu_orphan(SimpleNamespace(pid=1, ppid=5, ctime=0), by_pid, None, NOW, 95)


def test_orphan_has_no_name_restriction_and_never_raises() -> None:
    assert cs.is_cpu_orphan(_row(name="whatever"), {}, None, NOW, 95)
    assert cs.is_cpu_orphan(None, {}, None, NOW, 95) is False


def test_thresholds_defaults_and_overrides() -> None:
    d = cs.thresholds({})
    assert (d["busy_pct"], d["ticks"], d["proc_core_pct"]) == (90.0, 3, 80.0)
    assert d["protected"] == cs.DEFAULT_PROTECTED
    t = cs.thresholds({
        "box.saturation_busy_pct": "85", "box.saturation_ticks": "5",
        "box.saturation_proc_core_pct": "garbage", "box.saturation_protected": "Foo, bar",
    })
    assert (t["busy_pct"], t["ticks"], t["proc_core_pct"]) == (85.0, 5, 80.0)
    assert t["protected"] == ("foo", "bar")
