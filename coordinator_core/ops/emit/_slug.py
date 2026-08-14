"""
coordinator_core.ops.emit._slug — shared hostname slug helper.

Single source of truth for ``_machine_slug``, which both recorder.py
(backlog.record) and goal_append.py (goal.append) must agree on.

Port of: append-goal-event.sh (DoE b5a4192c, 2026-07-20). Mirrors its slug algorithm
exactly: lowercase → collapse every run of non-[a-z0-9] to '-' → strip leading/trailing '-'

Having one implementation means one test point and one update site when the bash
convention changes.  Two divergent copies silently produce different shard filenames
on the same machine, fragmenting data — see F3 in the tc-3 review.
"""

from __future__ import annotations

import re
import socket
from typing import Optional

# Compiled once; both callers import this module's constant.
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def machine_slug(hostname: Optional[str] = None) -> str:
    """Return the hostname slug used in per-machine JSONL shard filenames.

    Port of: append-goal-event.sh (DoE b5a4192c, 2026-07-20). Mirrors its algorithm
    exactly: lowercase, collapse every run of non-``[a-z0-9]`` characters to a single
    '-', then strip leading/trailing '-'.
    Falls back to socket.gethostname() when hostname is None; returns "unknown" for
    empty / whitespace-only / dash-only results.
    """
    raw = hostname if hostname is not None else socket.gethostname()
    raw = (raw or "unknown").lower()
    slug = _SLUG_RE.sub("-", raw).strip("-")
    return slug or "unknown"
