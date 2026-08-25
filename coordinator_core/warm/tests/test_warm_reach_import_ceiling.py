"""Pins AC3 of `docs/plans/2026-08-23-no-hook-fire-pays-an-interpreter-start.md`:
the warm-reach entry point costs under 20ms of import CPU against a cold
interpreter, and pulls neither the `coordinator_core.hooks` nor the
`coordinator_core.ops` graph.

`warm.entry_seam :: try_warm_guard_dispatch` is that entry point. C2's
`hook_reach.py` — the cold-cheap door-knocker AC3 was originally written
against — was dropped in the plan's 2026-08-23 reshape along with C3, so
`entry_seam` is what a hook fire actually reaches on the adopted transport.

WHY MIN-OF-N RATHER THAN A SINGLE RUN OR A MEAN. This box runs 50-70
concurrent sessions, and that peer load lands on any single sample as
scheduling wait the importer did not spend. The minimum over a small batch is
the least contaminated estimator available here, and is the statistic this
workstream's own budget-manifest records for the same reason. A mean would
encode the neighbours; a single run is a coin flip.

Negative-spec:
  - Does NOT measure wall clock of a whole invocation. `-X importtime` reports
    per-module import time, which is the quantity AC3 names.
  - Does NOT assert a floor. A future import that gets cheaper is not a
    regression, and a test that pins both ends fails on every unrelated win.
  - Does NOT import the entry point into the test process to measure it.
    Import is once-per-process; a second import inside an already-warm
    interpreter measures nothing and reads as a spectacular pass.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Dict, List

import pytest

from coordinator_core.win_portability import no_console_creationflags

#: AC3's stated budget. Not a round number chosen for comfort -- the criterion
#: names 20ms, and the bar moves only when the plan's does.
CEILING_MS = 20.0

#: The graphs AC3 forbids. Populating an op registry is the client-side cost the
#: whole transport change exists to stop paying, so reaching either of these from
#: the warm-reach path defeats the point regardless of what the clock says.
FORBIDDEN_PREFIXES = ("coordinator_core.ops", "coordinator_core.hooks")

ENTRY_POINT = "coordinator_core.warm.entry_seam"
IMPORT_STMT = "from coordinator_core.warm.entry_seam import try_warm_guard_dispatch"

_REPEATS = 3


def _importtime_run() -> Dict[str, float]:
    """One cold interpreter under `-X importtime`; returns cumulative ms per module.

    `-X importtime` writes to stderr as `import time: self | cumulative | name`,
    with nesting encoded as leading spaces on the name. The header line's
    non-numeric fields are skipped rather than parsed defensively -- a format
    change should surface as a failure here, not as a silently empty mapping.
    """
    proc = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", IMPORT_STMT],
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )
    timings: Dict[str, float] = {}
    for line in proc.stderr.splitlines():
        fields = line.split("|")
        if len(fields) != 3:
            continue
        try:
            cumulative_us = int(fields[1])
        except ValueError:
            continue
        timings[fields[2].strip()] = cumulative_us / 1000.0
    return timings


def _batch() -> List[Dict[str, float]]:
    return [_importtime_run() for _ in range(_REPEATS)]


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_warm_reach_entry_point_imports_under_the_ceiling():
    runs = _batch()
    observed = [r[ENTRY_POINT] for r in runs if ENTRY_POINT in r]
    assert observed, (
        f"{ENTRY_POINT} never appeared in -X importtime output -- the entry point "
        "moved, or the importtime format changed"
    )
    best = min(observed)
    assert best < CEILING_MS, (
        f"warm-reach entry point costs {best:.1f}ms of import CPU (min of "
        f"{_REPEATS}), over AC3's {CEILING_MS}ms ceiling. Read the -X importtime "
        "tree for the dominant child: a heavy stdlib module imported at module "
        "scope for one call inside one function is how this budget goes twice "
        "over, and is what `asyncio` did here before it was deferred into "
        "`reentrant_dispatch`."
    )


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_warm_reach_entry_point_pulls_neither_forbidden_graph():
    reached = {
        name
        for run in _batch()
        for name in run
        if name.startswith(FORBIDDEN_PREFIXES)
    }
    assert not reached, (
        "the warm-reach entry point pulled forbidden module(s): "
        f"{sorted(reached)}. Importing either graph registers every op via "
        "package __init__ side effects -- the client-side cost this transport "
        "exists to stop paying, and it is paid before any code decides to skip it"
    )
