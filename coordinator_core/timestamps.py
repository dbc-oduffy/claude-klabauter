"""Rendering stored timestamps so a reader cannot compare them to the wrong clock.

Every durable record in this tree stamps UTC. Almost every surface a reader
checks a record against -- the Windows process table, `Get-Process`, an `ls`
listing, a session's own sense of "now" -- is local. The offset is usually
present in the stamp and is misread anyway, because reading a timestamp is not
the same act as subtracting one.

Measured on 2026-09-02: a publish lock stamped `19:00:02.830245+00:00` was read
as seventy minutes old against a local clock and had a stale-lock diagnosis
built on it before the holder was checked and found alive at ten minutes. The
same session then twice more had to stop and convert by hand -- once on a watch
heartbeat whose stamp appeared to precede the process that wrote it.

So the rule is not "print a clearer timestamp". A clearer timestamp is still
subtracted against the wrong clock. **An age cannot be read in the wrong zone**,
so every timestamp reaching a reader carries one.

NEGATIVE SPEC -- what this module deliberately does not do:

- It does not guess. An unparseable, naive, absent or non-string stamp renders
  with an explicit unreadable marker and never a computed age, because a wrong
  age is worse than none: it is precise enough to sound measured, which is the
  least-checked kind of wrong number.
- It does not reformat or normalise the stamp itself. The stored bytes are
  reproduced verbatim so a reader can still grep the record for what they see.
- It does not convert to local time. Two renderings of one instant is the
  ambiguity, not the cure.
- It is never rendered into a PERSISTED artifact -- a goal file, a cache, a
  frontmatter value, an argument to another tool. An age is true only at the
  moment it is written, so baking one into a durable file produces a
  precise-sounding wrong number by tomorrow: the exact failure this module
  exists to stop, wearing a measurement's clothes. Persisted stamps stay bare
  and are aged by whoever reads them. The boundary is reader-facing output,
  not "a human might see it".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

UNREADABLE_AGE = "age unreadable"


def age_seconds(stamp: Any, now_epoch: Optional[float] = None) -> Optional[float]:
    """Seconds between `stamp` and now, or `None` when the record cannot say.

    Parses the stamp as the UTC it declares. Accepts both shapes this tree
    writes -- a trailing `Z` and an explicit `+00:00` offset, with or without
    microseconds. A naive stamp returns `None` rather than being assumed UTC:
    a stamp that never said which zone it meant is exactly the input this
    module exists to stop people guessing about.
    """
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    reference = (
        datetime.now(timezone.utc)
        if now_epoch is None
        else datetime.fromtimestamp(now_epoch, timezone.utc)
    )
    return (reference - parsed).total_seconds()


def age_phrase(seconds: float) -> str:
    """`41 minutes`, `3 seconds` -- a duration a reader takes without a unit key."""
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f} seconds"
    if seconds < 5400:
        return f"{seconds / 60:.0f} minutes"
    return f"{seconds / 3600:.1f} hours"


# Review: overengineering-reviewer -- this pair (plus `day_phrase`) is a
# general calendar-age facility with exactly one caller in the tree today
# (`with_age_date`, itself serving only `relocation_ledger.retired_at`).
# Kept private rather than deleted: privatizing matches the module's actual
# exported surface to its actual consumers without foreclosing a second date
# field, which is the trigger to re-generalise, not the first.
def _age_days(stamp: Any, now_epoch: Optional[float] = None) -> Optional[float]:
    """Days between a `YYYY-MM-DD` calendar date and now, or `None`.

    Separate from `age_seconds` on purpose. A bare date is a DAY, not an
    instant, so `age_seconds` refuses it along with every other zone-less
    stamp -- correctly, because guessing an instant out of it would be a
    guess. A caller that reaches for this one has declared that its field is
    a date, which is the fact `age_seconds` has no way to know; the day is
    read as UTC and the answer is rendered in days, so the sub-day ambiguity
    stays below the resolution of what is reported.
    """
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        parsed = datetime.strptime(stamp, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    reference = (
        datetime.now(timezone.utc)
        if now_epoch is None
        else datetime.fromtimestamp(now_epoch, timezone.utc)
    )
    return (reference - parsed).total_seconds() / 86400.0


def _day_phrase(days: float) -> str:
    """`today`, `1 day`, `36 days` -- a whole-day count, never a false decimal."""
    whole = int(max(0.0, days))
    if whole == 0:
        return "today"
    if whole == 1:
        return "1 day"
    return f"{whole} days"


def with_age_date(stamp: Any, now_epoch: Optional[float] = None) -> str:
    """A `YYYY-MM-DD` field as stored, followed by how long ago that day was.

    The date-field counterpart of `with_age`. Same contract in every other
    respect: the stored bytes are reproduced verbatim, nothing is converted,
    and a date that cannot be read renders marked rather than guessed at.
    """
    days = _age_days(stamp, now_epoch)
    if days is None:
        return f"{stamp} ({UNREADABLE_AGE})"
    if int(max(0.0, days)) == 0:
        return f"{stamp} ({_day_phrase(days)})"
    return f"{stamp} ({_day_phrase(days)} ago)"


def with_age(stamp: Any, now_epoch: Optional[float] = None) -> str:
    """The stamp as stored, followed by how long ago that was.

    The rendering every reader-facing surface should use in place of a bare
    field value. A stamp that cannot be aged still renders, marked, rather than
    being suppressed -- a reader who can see the raw value can still act on it,
    and a missing field would hide that the record carried anything at all.
    """
    age = age_seconds(stamp, now_epoch)
    if age is None:
        return f"{stamp} ({UNREADABLE_AGE})"
    return f"{stamp} ({age_phrase(age)} ago)"
