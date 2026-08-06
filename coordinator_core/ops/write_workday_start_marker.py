"""
coordinator_core.ops.write_workday_start_marker — day-cadence owner of
`state/.workday-start-marker`.

Purpose: this is the real writer `readers_health_reaper._read_marker_freshness`'s
day-cadence `d-workday-marker-write` directive names as its `cli`. Before this
module existed, that directive named `cli: "workday-start"` — a ceremony/skill
name, not a bin CLI, so nothing could ever execute it (removed as a category
error, commit 14076862). That removal was only half the fix: it left
`state/.workday-start-marker` with NO writer anywhere in the tree (grep it),
so the marker goes stale forever and `j-session-day-review-due` false-nags on
every session. This module is the missing other half — a real, resolvable CLI
that performs the write the module's own history claimed already happened.

Idempotent: writes today's local date (`coordinator_core.daily_day.local_day`)
into the marker file only when it isn't already fresh; a same-day re-run is a
no-op, not a rewrite.

State-root resolution is NOT re-derived here — it reuses
`coordinator_core.ops.check_weekly_staleness._resolve_state_root`, the exact
seam `readers_health_reaper._read_marker_freshness` itself reads through, so
the writer and the reader agree on "where" by construction rather than by two
independently-maintained copies drifting apart.

Spec backlink: docs/plans/2026-07-24-computed-skills-b2-ceremony-start.md § Reader-in-process port scoping, chunk C2d
               coordinator_core/orient_assemble/readers_health_reaper.py

Negative-spec:
    - Does NOT re-derive state-root resolution — imports
      `check_weekly_staleness._resolve_state_root` rather than duplicating
      the DR-047/stop-the-rot Rule-5 default-branch logic a third time.
    - Does NOT touch `state/week-changelog/HEADER.md` or any week-cadence
      surface — day-cadence marker only.
    - Does NOT run when the state root can't be resolved (no git root, no
      claude-klabauter root under the meta-repo case) — fails open (exit 0, stderr
      note), mirroring every sibling ops module's fail-open contract; a
      once-a-day bookkeeping write is never a gate.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from coordinator_core.daily_day import local_day
from coordinator_core.ops.check_weekly_staleness import _resolve_state_root
from coordinator_core.session.declared_writes import declare_write

_MARKER_FILENAME = ".workday-start-marker"


def write_marker() -> "tuple[str, int]":
    """Perform the idempotent write. Returns (stdout_text, rc); rc is always
    0 — a marker-write is bookkeeping, never a gate, so failure to resolve
    the state root degrades to a stderr note rather than a nonzero exit."""
    state_root_str = _resolve_state_root()
    if not state_root_str:
        sys.stderr.write(
            "write-workday-start-marker: could not resolve state root — skipping\n"
        )
        return ("", 0)

    today = local_day()
    marker_path = Path(state_root_str) / _MARKER_FILENAME

    existing = ""
    if marker_path.is_file():
        existing = marker_path.read_text(encoding="utf-8", errors="replace").strip()
    if existing == today:
        return (f"workday-start-marker: already fresh ({today})\n", 0)

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(today, encoding="utf-8")
    declare_write(marker_path)
    return (f"workday-start-marker: written ({today})\n", 0)


def main(argv: List[str]) -> int:
    """CLI entry — no arguments; writes the marker and reports what happened."""
    if argv:
        sys.stderr.write("usage: write-workday-start-marker.py (no arguments)\n")
        return 2
    text, rc = write_marker()
    if text:
        sys.stdout.write(text)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
