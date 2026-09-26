"""
daily_day.py

Port of: coordinator-daily-day.sh (DoE c6d97219, 2026-07-22)
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

CEREMONY DAY ANCHOR (Item 12, 2026-09-26): `local_day()` reads the
`ceremony_day_anchor` key from repo-root `coordinator.local.md`, once per
process, in the reader shape of
`coordinator_core.resolve_validation_cmd.cs_read_local_md_key`. The default
(unset, or any value other than `utc`) is `local`, byte-identical to the
function's pre-Item-12 behaviour. `utc` switches the returned day to the UTC
calendar day. Every caller of `local_day()` inherits the anchor automatically
— no caller threads a parameter through to get it.
"""

from __future__ import annotations

import datetime
import os

# Cache keyed by repo_root: "read once per process" without denying a test
# (or a multi-root host process) the ability to see a different root's own
# setting. A single-root process — the overwhelmingly common case — reads
# coordinator.local.md exactly once.
_ANCHOR_CACHE: dict[str, str] = {}


def _read_ceremony_day_anchor(repo_root: str) -> str:
    # Local import: avoids a module-load-time dependency from this
    # near-leaf module onto resolve_validation_cmd's heavier import surface.
    from coordinator_core.resolve_validation_cmd import cs_read_local_md_key

    raw = cs_read_local_md_key(repo_root, "ceremony_day_anchor").strip().lower()
    return "utc" if raw == "utc" else "local"


def _ceremony_day_anchor(repo_root: str | None = None) -> str:
    root = repo_root if repo_root is not None else os.getcwd()
    cached = _ANCHOR_CACHE.get(root)
    if cached is None:
        cached = _read_ceremony_day_anchor(root)
        _ANCHOR_CACHE[root] = cached
    return cached


def local_day(repo_root: str | None = None) -> str:
    """Return today's date under the `ceremony_day_anchor`, as YYYY-MM-DD.

    Default anchor (`local`, unset): mirrors `date -I` / `date +%Y-%m-%d`
    (local-timezone, calendar-day granularity) from the bash original —
    Python's datetime.date.today() is already local-TZ by construction, so
    no BSD/GNU portability fallback is needed here (that fallback existed in
    bash only because `date -I` is GNU-only; not applicable in Python).

    `utc` anchor: returns the UTC calendar day instead, via
    datetime.datetime.now(datetime.timezone.utc).date() — never
    datetime.date.today(), which is local-TZ by construction and would
    silently ignore the anchor.

    `repo_root` lets a caller pin which coordinator.local.md is read (tests,
    or a process operating across repo roots); ordinary callers omit it and
    get the anchor for `os.getcwd()`.
    """
    if _ceremony_day_anchor(repo_root) == "utc":
        return datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    return datetime.date.today().isoformat()
