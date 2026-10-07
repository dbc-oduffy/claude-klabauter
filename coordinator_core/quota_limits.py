"""Shared reader for the harness's refusal record on a transcript line.

Two structured spellings, nothing else: a `quotaLimits` object with
`status: rejected` and a `resetsAt` epoch, and a top-level `error: rate_limit`
with `apiErrorStatus: 429`. Prose quoting the user-facing limit sentence is
never a match. Pure string/regex work: no JSON parse, no spawn.
"""

from __future__ import annotations

import re
from typing import Optional

#: `[^{}]*` holds because `quotaLimits` carries no nested object; a nested one
#: fails the match, which reads as "no refusal". Each pattern requires an
#: UNESCAPED quote before its key so a JSON blob quoted inside a message body
#: cannot inject one.
_QUOTA_LIMITS = re.compile(r'(?<!\\)"quotaLimits"\s*:\s*\{([^{}]*)\}')
_QUOTA_REJECTED = re.compile(r'(?<!\\)"status"\s*:\s*"rejected"')
_QUOTA_RESETS_AT = re.compile(r'(?<!\\)"resetsAt"\s*:\s*(\d{9,12})')
_ERROR_RATE_LIMIT = re.compile(r'(?<!\\)"error"\s*:\s*"rate_limit"')
_API_STATUS_429 = re.compile(r'(?<!\\)"apiErrorStatus"\s*:\s*429\b')


def rejected_resets_at(line: str) -> Optional[float]:
    """The `resetsAt` epoch of a rejected `quotaLimits` object on `line`, else None."""
    if '"quotaLimits"' not in line:
        return None
    body = _QUOTA_LIMITS.search(line)
    if not body or not _QUOTA_REJECTED.search(body.group(1)):
        return None
    resets = _QUOTA_RESETS_AT.search(body.group(1))
    return float(resets.group(1)) if resets else None


def is_rate_limit_error(line: str) -> bool:
    """True for a harness `error: rate_limit` turn carrying `apiErrorStatus` 429."""
    return bool(_ERROR_RATE_LIMIT.search(line) and _API_STATUS_429.search(line))


def active_rate_limit(line: str, now: float) -> Optional[float]:
    """`resetsAt` of a live refusal on `line`: a 429 rate_limit turn whose rejected window has not lifted.

    None when the line is not that turn or the reset is already past.
    """
    resets_at = rejected_resets_at(line)
    if resets_at is None or resets_at <= now or not is_rate_limit_error(line):
        return None
    return resets_at
