
from __future__ import annotations

import datetime
import re

import coordinator_core.daily_day as daily_day
from coordinator_core.daily_day import local_day


def setup_function(_fn):
    # local_day() caches the anchor per-repo_root ("read once per process");
    # tests exercise different anchors under the same cwd, so each test
    # starts from a clean cache.
    daily_day._ANCHOR_CACHE.clear()


def test_local_day_format_matches_yyyy_mm_dd():
    result = local_day()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", result), result


def test_local_day_matches_python_local_today():
    expected = datetime.date.today().isoformat()
    assert local_day() == expected


def test_local_day_default_anchor_is_byte_identical_to_pre_item_12(monkeypatch):
    # No coordinator.local.md ceremony_day_anchor key -> "local", byte-identical
    # to today's (pre-Item-12) local_day() output.
    monkeypatch.setattr(daily_day, "_read_ceremony_day_anchor", lambda repo_root: "local")
    expected = datetime.date.today().isoformat()
    assert local_day() == expected


def test_local_day_utc_anchor_across_a_local_utc_midnight_split(monkeypatch):
    # Pick an instant that is one calendar day apart in UTC vs a
    # far-west local timezone, to prove the utc anchor reads the UTC
    # calendar day rather than datetime.date.today() (local-TZ by
    # construction, and therefore anchor-blind if called directly).
    utc_now = datetime.datetime(2026, 1, 2, 0, 30, tzinfo=datetime.timezone.utc)

    class _FixedDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return utc_now.astimezone(tz)
            return utc_now.replace(tzinfo=None)

    monkeypatch.setattr(daily_day, "_read_ceremony_day_anchor", lambda repo_root: "utc")
    monkeypatch.setattr(daily_day.datetime, "datetime", _FixedDateTime)

    assert local_day() == "2026-01-02"


def test_local_day_reads_anchor_once_per_process_repo_root(monkeypatch):
    calls = []

    def _spy(repo_root):
        calls.append(repo_root)
        return "local"

    monkeypatch.setattr(daily_day, "_read_ceremony_day_anchor", _spy)
    local_day()
    local_day()
    local_day()
    assert len(calls) == 1


def test_iso_week_grouping_is_coherent_under_the_utc_anchor(monkeypatch):
    # Under the utc anchor, local_day()'s own returned day and an
    # isocalendar() grouping derived from the same UTC instant land in the
    # same ISO week -- the coherence property Item 12 exists to guarantee
    # for callers (e.g. rollups.py) that group facts by ISO week.
    utc_now = datetime.datetime(2026, 1, 1, 23, 0, tzinfo=datetime.timezone.utc)

    class _FixedDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return utc_now.astimezone(tz)
            return utc_now.replace(tzinfo=None)

    monkeypatch.setattr(daily_day, "_read_ceremony_day_anchor", lambda repo_root: "utc")
    monkeypatch.setattr(daily_day.datetime, "datetime", _FixedDateTime)

    day = local_day()
    expected_week = utc_now.date().isocalendar()[:2]
    assert datetime.date.fromisoformat(day).isocalendar()[:2] == expected_week
