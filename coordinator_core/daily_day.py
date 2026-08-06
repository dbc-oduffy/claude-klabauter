"""
daily_day.py

Port of: coordinator-daily-day.sh (example-doctrine-repo c6d97219, 2026-07-22)
Spec backlink: docs/plans/2026-06-26-datetime-handling-coherence.md § D1, C-A1

Purpose: exposes local_day(), the single authoritative source for today's date in
the operator's LOCAL timezone. All anchor-sensitive ceremony day-keys (changelog
filenames, daily-branch names, plan filenames, commit-window bounds) MUST derive
from this function, not from UTC-now.

Rationale: a UTC-anchored `today` is wrong for operators in UTC-4..UTC+14 timezones
once a session crosses midnight or runs in the morning before noon. The PM ratified
local-day-everywhere on 2026-06-26 (plan D1).

INSTANT TIMESTAMPS: event/record timestamps that must be globally comparable
(handoff `dispatched_at`, artifact `created_at`, audit log lines) stay UTC and
MUST NOT use this function. This function is day-granularity LOCAL only.
"""

from __future__ import annotations

import datetime


def local_day() -> str:
    """Return today's date in the operator's LOCAL timezone, as YYYY-MM-DD.

    Mirrors `date -I` / `date +%Y-%m-%d` (local-timezone, calendar-day granularity)
    from the bash original — Python's datetime.date.today() is already local-TZ by
    construction, so no BSD/GNU portability fallback is needed here (that fallback
    existed in bash only because `date -I` is GNU-only; not applicable in Python).
    """
    return datetime.date.today().isoformat()
