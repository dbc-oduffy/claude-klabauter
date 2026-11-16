from types import SimpleNamespace

from coordinator_core.box_memory import pressure as p
from pathlib import Path
import pytest


def R(avail, trusted: bool = True) -> SimpleNamespace:
    return SimpleNamespace(avail_mb=avail, trusted=trusted)


def test_classify_both_sides() -> None:
    assert p.classify(R(1000), 2000) == p.LOW
    assert p.classify(R(2000), 2000) == p.OK
    assert p.classify(R(None), 2000) == p.UNKNOWN
    assert p.classify(R(100, trusted=False), 2000) == p.UNKNOWN


_UNIT = 10_000_000
_NOW = 10_000 * _UNIT


def row(pid, name, age_s, ppid: int = 999_999) -> SimpleNamespace:
    return SimpleNamespace(pid=pid, ppid=ppid, name=name, ctime=_NOW - age_s * _UNIT)


def _units(monkeypatch) -> None:
    from coordinator_core.bash_guards import _host_probe

    monkeypatch.setattr(_host_probe, "ctime_units_per_second", lambda: _UNIT)


class _Dead:
    @staticmethod
    def creation_time(pid) -> None:
        return None


def test_storm_threshold_both_sides(monkeypatch: pytest.MonkeyPatch) -> None:
    _units(monkeypatch)
    census = [row(i, "grep.exe", 3600) for i in range(3)] + [
        row(10, "find", 3600), row(11, "python.exe", 3600), row(12, "bash.exe", 3600)]
    assert p.storm_count(census, _Dead, _NOW) == 4
    assert p.storm(census, 4, _Dead, _NOW)
    assert not p.storm(census, 5, _Dead, _NOW)


def test_young_dead_parent_searches_never_count(monkeypatch: pytest.MonkeyPatch) -> None:
    _units(monkeypatch)
    young = [row(i, "grep.exe", 60) for i in range(20)]
    assert p.storm_count(young, _Dead, _NOW) == 0
    just_under = [row(1, "grep", 30 * 60 - 1)]
    at_floor = [row(2, "grep", 30 * 60)]
    assert p.storm_count(just_under, _Dead, _NOW) == 0
    assert p.storm_count(at_floor, _Dead, _NOW) == 1


def test_aged_search_under_live_parent_or_unknown_ctime_never_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    _units(monkeypatch)
    parent = SimpleNamespace(pid=5, ppid=1, name="bash", ctime=_NOW - 7200 * _UNIT)
    live_child = row(6, "grep", 3600, ppid=5)
    zero = row(7, "grep", 3600)
    zero.ctime = 0
    assert p.storm_count([parent, live_child, zero], _Dead, _NOW) == 0
    assert p.storm_count(["grep.exe"] * 9, _Dead, _NOW) == 0
    assert p.storm_count(None) == 0


def test_now_ctime_is_a_plausible_current_instant() -> None:
    import time

    a = p.now_ctime()
    time.sleep(0.01)
    assert p.now_ctime() > a > 0


def test_remedy_text(tmp_path: Path) -> None:
    (tmp_path / "trim-file-cache.py").write_text("")
    t = p.remedy_text(p.KIND_LOW_MEMORY, tmp_path)
    assert t.startswith("GOVERNOR CACHE-TRIM-HINT run elevated: python ")
    assert t.endswith("trim-file-cache.py\" --apply")
    assert "unavailable" in p.remedy_text(p.KIND_LOW_MEMORY, tmp_path / "nope")
    storm = p.remedy_text(p.KIND_GREP_STORM)
    assert "reap" in storm and "30 min" in storm
