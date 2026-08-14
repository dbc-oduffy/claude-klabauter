"""
test_daily_day.py — pytest coverage for coordinator_core.daily_day.

Port of: coordinator-daily-day.sh (DoE c6d97219, 2026-07-22)
"""

from __future__ import annotations

import datetime
import re

from coordinator_core.daily_day import local_day


def test_local_day_format_matches_yyyy_mm_dd():
    result = local_day()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", result), result


def test_local_day_matches_python_local_today():
    expected = datetime.date.today().isoformat()
    assert local_day() == expected
