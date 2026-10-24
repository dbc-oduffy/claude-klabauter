"""Run-id stamp parsing: the epoch a `<kind>-<YYYYMMDDTHHMMSS>-<hex6>` run id was minted at."""

from __future__ import annotations

import re
from calendar import timegm
from typing import Optional

_STAMP = re.compile(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})-[0-9a-f]{6}$")


def _stamp_epoch(run_id: str) -> Optional[float]:
    match = _STAMP.search(run_id)
    if match is None:
        return None
    try:
        y, mo, d, h, mi, s = (int(g) for g in match.groups())
        return float(timegm((y, mo, d, h, mi, s, 0, 0, 0)))
    except (ValueError, OverflowError):
        return None
