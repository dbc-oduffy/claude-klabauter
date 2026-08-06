"""
coordinator_core.benchmarks._read_events_probe -- Minimal spawn target for the
C5 read_events cold-start measurement (AC3).

Purpose: the entire body of what gets timed end-to-end (interpreter cold
start + import + `coordinator_core.tracker_store.read_events()`) by
`measure_read_events.py`. `read_events` is a plain library call, not a
registered `coordinator_core.invoke` op (see `tracker_store.py`'s
negative-spec "Do NOT grow read_events into a query surface (DEC-12)"), so
there is no `invoke <op> <params>` entrypoint to spawn against. This probe
is that entrypoint's stand-in: a one-shot script invoked via
`python -m coordinator_core.benchmarks._read_events_probe --repo <path>`,
timed from the OUTSIDE (subprocess spawn to exit) by
`measure_read_events._time_probe`, exactly as `timer.time_invocation` times
a real op invocation -- this module intentionally does no internal timing of
its own so the measured wall-clock band matches the real cold-start shape a
caller would experience.

Spec backlink: docs/plans/2026-07-28-sat-01-sovereign-tracker-substrate.md § C5 (AC3).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from coordinator_core import tracker_store


def main() -> int:
    """Parse `--repo`, read every shard under it, print the event count."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()

    events = tracker_store.read_events(repo_root=Path(args.repo))
    print(len(events))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
