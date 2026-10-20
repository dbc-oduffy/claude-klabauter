"""Tests for coordinator_core.cadence.period_mode (AC 1)."""

from __future__ import annotations

import json
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from coordinator_core.cadence import period_mode as pm


@pytest.fixture(autouse=True)
def settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    (tmp_path / "settings" / "machine-local").mkdir(parents=True)
    pm._clear_registry_cache()
    yield tmp_path / "settings" / "machine-local"
    pm._clear_registry_cache()


def _registry(mdir: Path, text: str) -> None:
    (mdir / "registry.local.toml").write_text(text, encoding="utf-8")
    pm._clear_registry_cache()


# 2026-10-13 is a Tuesday: a non-Monday anchor.
ANCHOR = date(2026, 10, 13)


def test_explicit_always_wins():
    r = pm.resolve_period_mode("day", datetime(2026, 10, 20, 9, 0), "conclude", week_anchor=ANCHOR)
    assert (r.mode, r.source) == ("conclude", "explicit")
    r = pm.resolve_period_mode("day", datetime(2026, 10, 20, 18, 0), "catch_up", week_anchor=ANCHOR)
    assert (r.mode, r.source) == ("catch_up", "explicit")
    r = pm.resolve_period_mode("week", _anchor_dt(6), "catch_up", week_anchor=ANCHOR)
    assert r.mode == "catch_up"


def _anchor_dt(days: int) -> datetime:
    return datetime.combine(ANCHOR + timedelta(days=days), datetime.min.time()).replace(hour=12)


def test_day_boundary_1459_vs_1500():
    a = pm.resolve_period_mode("day", datetime(2026, 10, 20, 14, 59), None, week_anchor=ANCHOR)
    b = pm.resolve_period_mode("day", datetime(2026, 10, 20, 15, 0), None, week_anchor=ANCHOR)
    assert (a.mode, a.source) == ("catch_up", "auto-clock")
    assert b.mode == "conclude"
    assert a.in_progress == "2026-10-20"


def test_day_threshold_from_registry(settings_home):
    _registry(settings_home, '"coordinator.cadence.day_conclude_after" = "09:30"\n')
    r = pm.resolve_period_mode("day", datetime(2026, 10, 20, 9, 30), None, week_anchor=ANCHOR)
    assert r.mode == "conclude"


def test_unparseable_threshold_is_reason_not_raise(settings_home):
    _registry(
        settings_home,
        '"coordinator.cadence.day_conclude_after" = "teatime"\n'
        '"coordinator.cadence.week_conclude_after_days" = "many"\n',
    )
    d = pm.resolve_period_mode("day", datetime(2026, 10, 20, 14, 59), None, week_anchor=ANCHOR)
    assert d.mode == "catch_up" and "unparseable" in d.reason
    w = pm.resolve_period_mode("week", _anchor_dt(3), None, week_anchor=ANCHOR)
    assert w.mode == "catch_up" and "unparseable" in w.reason


def test_week_nonmonday_anchor_boundary():
    assert ANCHOR.weekday() == 1
    a = pm.resolve_period_mode("week", _anchor_dt(3), None, week_anchor=ANCHOR)
    b = pm.resolve_period_mode("week", _anchor_dt(4), None, week_anchor=ANCHOR)
    assert a.mode == "catch_up" and b.mode == "conclude"
    assert a.in_progress == "2026-10-13"


def test_week_threshold_from_registry(settings_home):
    _registry(settings_home, '"coordinator.cadence.week_conclude_after_days" = 2\n')
    r = pm.resolve_period_mode("week", _anchor_dt(2), None, week_anchor=ANCHOR)
    assert r.mode == "conclude"


def test_week_missed_across_two_week_span():
    now = _anchor_dt(15)  # third week, day 1
    r = pm.resolve_period_mode("week", now, None, week_anchor=ANCHOR)
    assert r.in_progress == "2026-10-27"
    assert r.missed == ("2026-10-13", "2026-10-20")
    r = pm.resolve_period_mode(
        "week", now, None, week_anchor=ANCHOR, concluded=["2026-10-13"]
    )
    assert r.missed == ("2026-10-20",)


def test_day_missed_lists_unconcluded_days_excluding_today():
    r = pm.resolve_period_mode(
        "day",
        datetime(2026, 10, 20, 0, 30),
        None,
        week_anchor=ANCHOR,
        since=date(2026, 10, 17),
        concluded=["2026-10-18"],
    )
    assert r.missed == ("2026-10-17", "2026-10-19")
    assert r.mode == "catch_up"


def test_day_missed_empty_without_since():
    r = pm.resolve_period_mode("day", datetime(2026, 10, 20, 9, 0), None, week_anchor=ANCHOR)
    assert r.missed == ()


def test_week_window_chain_not_monday():
    start, end = pm.week_window(date(2026, 10, 27), ANCHOR)
    assert (start, end) == (date(2026, 10, 27), date(2026, 11, 3))
    start, end = pm.week_window(date(2026, 10, 26), ANCHOR)
    assert (start, end) == (date(2026, 10, 20), date(2026, 10, 27))
    assert pm.week_window(date(2026, 10, 10), ANCHOR)[0] == date(2026, 10, 6)


def test_to_dict_shape():
    r = pm.resolve_period_mode("day", datetime(2026, 10, 20, 9, 0), None, week_anchor=ANCHOR)
    assert set(r.to_dict()) == {"mode", "source", "in_progress", "missed", "reason"}
    assert isinstance(r.to_dict()["missed"], list)


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        pm.resolve_period_mode("month", datetime(2026, 10, 20), None, week_anchor=ANCHOR)
    with pytest.raises(ValueError):
        pm.resolve_period_mode("day", datetime(2026, 10, 20), "bogus", week_anchor=ANCHOR)


def test_no_subprocess(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    pm.resolve_period_mode("day", datetime(2026, 10, 20, 9, 0), None, week_anchor=ANCHOR)
    pm.resolve_period_mode("week", _anchor_dt(9), None, week_anchor=ANCHOR)


def test_concluding_directives_keys():
    assert set(pm.CONCLUDING_DIRECTIVES) == {"workday-complete", "workweek-complete"}
    assert len(pm.CONCLUDING_DIRECTIVES["workday-complete"]) == 5
    assert len(pm.CONCLUDING_DIRECTIVES["workweek-complete"]) == 7


def test_constants_match_doe_fragment():
    frag_path = (
        Path(__file__).resolve().parents[3].parent
        / "coordinator-content-repo/coordinator/contract/cadence-period-mode-fragment.json"
    )
    if not frag_path.is_file():
        pytest.skip("coordinator-content-repo fragment not present: engine-mirror parity NOT enforced on this box")
    frag = json.loads(frag_path.read_text(encoding="utf-8"))
    for name, value in frag["engine_mirror"]["constants"].items():
        assert getattr(pm, name) == value, name
    assert {k: list(v) for k, v in pm.CONCLUDING_DIRECTIVES.items()} == frag["concluding_directives"]
