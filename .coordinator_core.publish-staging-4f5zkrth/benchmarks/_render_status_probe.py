"""
coordinator_core.benchmarks._render_status_probe -- Minimal spawn target for
the C10 projection cold-start measurement (AC16/AC17).

Purpose: the entire body of what gets timed end-to-end (interpreter cold
start + import + `coordinator_core.tracker_projection.render_status()`) by
`measure_render_status.py`. Mirrors `_read_events_probe.py`'s shape exactly
(same one-shot `python -m` spawn target, timed from the OUTSIDE by the
caller) so the two measurements are directly comparable: `read_events`'s
own cold-start cost (sat-01 AC3) plus this module's is the full path a real
`render_status` caller pays.

`render_status` folds ALL of `read_events`' output on every call (see
`tracker_projection._fold_axis_states`'s docstring: "Built ONCE per call as
an in-memory dict -- never persisted, never reused across calls" -- DR-215
spawn-per-call has no process to cache across), so this probe does no
internal timing of its own -- exactly as `_read_events_probe.py` does not --
so the measured wall-clock band matches the real cold-start shape a caller
would experience.

Spec backlink: pln-sat-03-event-sourced-completio-c270a1
§ Tasks C10 (AC16, AC17).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from coordinator_core import tracker_projection


def main() -> int:
    """Parse `--repo` and `--item-id`, render that item's status, print it."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--item-id", required=True)
    args = parser.parse_args()

    status = tracker_projection.render_status(args.item_id, repo_root=Path(args.repo))
    print(status)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
