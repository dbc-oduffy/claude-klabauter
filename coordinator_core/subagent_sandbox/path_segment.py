"""Leaf path-segment sanitizer shared by `provision_report` and the Bash guards.

Stdlib-only so a guard that needs one safe segment does not import `provision_report`'s closure.
"""

from __future__ import annotations

import re
from typing import Optional

__all__ = ["SEGMENT_WHITELIST_RE", "sanitize_segment"]

#: `@` is admitted so the canonical `<name>@session-<short8>` agent id survives unchanged; it is
#: neither a directory separator nor a component of `.`/`..`.
SEGMENT_WHITELIST_RE = re.compile(r"[^A-Za-z0-9._@-]+")

#: Sanitized segments still rejected although the whitelist preserves dots.
_REJECTED_SEGMENTS = {"", ".", ".."}


def sanitize_segment(seg: str) -> Optional[str]:
    """Reduce `seg` to a single safe path segment, or None if unsafe.

    Each run of characters outside `[A-Za-z0-9._@-]` collapses to one '-', leading and trailing '-'
    are trimmed, and an empty, '.' or '..' result is rejected. Never calls `Path.resolve()`.
    """
    sanitized = SEGMENT_WHITELIST_RE.sub("-", seg).strip("-")
    if sanitized in _REJECTED_SEGMENTS:
        return None
    return sanitized
