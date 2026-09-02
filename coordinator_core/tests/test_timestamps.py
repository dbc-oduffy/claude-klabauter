"""Coverage for the one age-rendering helper every reader-facing surface uses.

The module's whole reason to exist is that a wrong age is worse than no age:
it is precise enough to sound measured, which is the least-checked kind of
wrong number. These cases pin the refusals as hard as the arithmetic.
"""

from __future__ import annotations

import calendar
import time
from datetime import datetime, timedelta, timezone

import pytest

from coordinator_core import timestamps


class TestAgeSeconds:
    def test_an_offset_stamp_ages_against_utc_not_the_local_clock(self):
        held = datetime.now(timezone.utc) - timedelta(seconds=600)
        assert 595 <= timestamps.age_seconds(held.isoformat()) <= 605

    def test_a_z_stamp_ages_identically_to_its_offset_spelling(self):
        held = datetime.now(timezone.utc) - timedelta(seconds=600)
        offset = timestamps.age_seconds(held.isoformat())
        z_form = timestamps.age_seconds(held.isoformat().replace("+00:00", "Z"))
        assert abs(offset - z_form) < 1.0

    @pytest.mark.parametrize(
        "stamp",
        ["2026-09-02T19:00:02", "unknown time", "", None, 17, "2026-09-02"],
        ids=["naive", "sentinel", "empty", "none", "nonstring", "date-only"],
    )
    def test_an_unusable_stamp_returns_none_rather_than_a_guess(self, stamp):
        assert timestamps.age_seconds(stamp) is None

    def test_the_watch_stamp_format_ages_as_calendar_timegm_did(self):
        """The prior `watch_heartbeat._tick_age_seconds` parsed exactly this
        shape with `calendar.timegm(time.strptime(...))`. Both readings of one
        stamp must land on the same instant, or collapsing the two helpers
        would have moved a liveness rendering."""
        stamp = "2026-09-02T17:12:05Z"
        now = time.time()
        legacy = now - calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
        assert abs(timestamps.age_seconds(stamp, now) - legacy) < 1e-6


class TestAgePhrase:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (-40.0, "0 seconds"),
            (3.0, "3 seconds"),
            (89.0, "89 seconds"),
            (600.0, "10 minutes"),
            (5399.0, "90 minutes"),
            (5400.0, "1.5 hours"),
        ],
    )
    def test_durations_render_without_a_unit_key(self, seconds, expected):
        assert timestamps.age_phrase(seconds) == expected


class TestWithAge:
    def test_the_stamp_is_reproduced_verbatim_beside_its_age(self):
        held = datetime.now(timezone.utc) - timedelta(seconds=600)
        rendered = timestamps.with_age(held.isoformat())
        assert rendered.startswith(held.isoformat())
        assert rendered.endswith("(10 minutes ago)")

    def test_an_unreadable_stamp_still_renders_marked_never_suppressed(self):
        assert timestamps.with_age("unknown time") == "unknown time (age unreadable)"

    def test_nothing_is_converted_to_local_time(self):
        """Two renderings of one instant is the ambiguity, not the cure."""
        stamp = "2026-09-02T19:00:02.830245+00:00"
        assert stamp in timestamps.with_age(stamp)


class TestDateFields:
    """`YYYY-MM-DD` fields (the relocation ledger's `retired_at`) are a DAY,
    not an instant -- `age_seconds` refuses them along with every other
    zone-less stamp, so they get their own declared entry point."""

    #: A fixed reading clock, so the expected day counts are arithmetic and
    #: not a function of when the suite runs.
    NOW = datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp()

    def test_a_date_ages_in_whole_days(self):
        assert timestamps.with_age_date("2026-07-28", self.NOW) == (
            "2026-07-28 (36 days ago)"
        )

    def test_one_day_is_not_pluralised(self):
        assert timestamps.with_age_date("2026-09-01", self.NOW) == "2026-09-01 (1 day ago)"

    def test_the_same_day_says_today_rather_than_zero_days_ago(self):
        assert timestamps.with_age_date("2026-09-02", self.NOW) == "2026-09-02 (today)"

    @pytest.mark.parametrize(
        "stamp",
        ["2026-09-02T19:00:02Z", "unknown", "", None, 17],
        ids=["instant", "sentinel", "empty", "none", "nonstring"],
    )
    def test_a_non_date_is_marked_rather_than_guessed(self, stamp):
        assert timestamps.with_age_date(stamp).endswith(f"({timestamps.UNREADABLE_AGE})")
