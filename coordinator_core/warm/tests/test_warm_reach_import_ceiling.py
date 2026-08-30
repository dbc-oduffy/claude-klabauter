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
  - Does NOT flatten the tree. `_parse_importtime_tree` keeps each row's DEPTH,
    and the ceiling assertion reports real children on failure. See below for
    why that is load-bearing rather than a nicety.

WHY THE TREE IS KEPT (2026-08-30). This module used to return
`Dict[name, cumulative_ms]`, discarding the indentation that encodes nesting --
while its own failure message said "read the -X importtime tree for the dominant
child". There was no tree to read, so the reader sorts the flat mapping and
attributes the top row. That is wrong twice over, and both traps were walked
into in one session by the EM who then wrote this paragraph:

  1. SIBLINGS READ AS PARENTS. `site` and the entry point are BOTH depth 0.
     Sorting flat makes `site` look like the entry point's dominant child, and
     "the failure is environmental, not our code" follows -- confidently, and
     backwards. Measured here: `site` 19.6ms and `entry_seam` 23.1ms are
     disjoint; the entry point owns every one of its 23.1ms.
  2. FIRST-PAYER ATTRIBUTION. In a cumulative profile the first importer of a
     shared dependency is charged its whole cost, and later importers look
     cheap. On this box `site` loads three byte-identical setuptools editable
     finders; the alphabetically first pays `pathlib` (~10ms) for all of them
     and shows ~12.5ms against ~0.3ms for its siblings. Reading that as "the
     first one is bad code" compares ordering, not quality. The discriminator
     is cheap and mandatory: change the order, or measure each in isolation --
     if the gap moves, the gap was the order.

Neither trap is specific to importtime. Any cumulative or cold-cache profile
(flame graphs, first-query-after-boot, cold-start benchmarks) charges the first
caller for shared setup, and any tree flattened into a sorted list stops
distinguishing a parent from a sibling.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Dict, List, NamedTuple

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

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


class _Node(NamedTuple):
    """One `-X importtime` row, with the nesting depth PRESERVED."""

    depth: int
    self_ms: float
    cumulative_ms: float
    name: str


def _parse_importtime_tree(stderr: str) -> List[_Node]:
    """Parse `-X importtime` stderr into depth-carrying rows, in emission order.

    The format is `import time: <self> | <cumulative> | <name>`, where nesting
    is encoded as TWO SPACES PER LEVEL prefixed to the name, after one leading
    separator space. Rows are emitted depth-first POST-order (children before
    their parent), which `_children_of` relies on.

    The header line's non-numeric fields are skipped rather than parsed
    defensively -- a format change should surface as a failure here, not as a
    silently empty result.
    """
    nodes: List[_Node] = []
    for line in stderr.splitlines():
        fields = line.split("|")
        if len(fields) != 3:
            continue
        try:
            self_us = int(fields[0].split(":")[-1])
            cumulative_us = int(fields[1])
        except ValueError:
            continue
        raw = fields[2]
        indented = raw[1:] if raw.startswith(" ") else raw
        depth = (len(indented) - len(indented.lstrip(" "))) // 2
        nodes.append(
            _Node(depth, self_us / 1000.0, cumulative_us / 1000.0, indented.strip())
        )
    return nodes


def _children_of(nodes: List[_Node], index: int) -> List[_Node]:
    """Direct children of `nodes[index]` -- rows at exactly depth+1 that precede
    it, back to the first row at depth <= its own.

    Walks BACKWARDS because importtime emits post-order. A node's own subtree is
    the contiguous run of deeper rows immediately before it.
    """
    depth = nodes[index].depth
    found: List[_Node] = []
    for j in range(index - 1, -1, -1):
        if nodes[j].depth <= depth:
            break
        if nodes[j].depth == depth + 1:
            found.append(nodes[j])
    return found


def _find(nodes: List[_Node], name: str) -> int:
    for i, node in enumerate(nodes):
        if node.name == name:
            return i
    return -1


def _importtime_run() -> List[_Node]:
    """One cold interpreter under `-X importtime`, parsed into a tree."""
    proc = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", IMPORT_STMT],
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )
    return _parse_importtime_tree(proc.stderr)


def _batch() -> List[List[_Node]]:
    return [_importtime_run() for _ in range(_REPEATS)]


def _breakdown(nodes: List[_Node], index: int, limit: int = 6) -> str:
    """The entry point's real children, costliest first -- what the ceiling
    message tells a reader to go and look at, rendered so they do not have to
    reconstruct it from a flat list and get it wrong."""
    kids = sorted(_children_of(nodes, index), key=lambda n: -n.cumulative_ms)[:limit]
    if not kids:
        return "    (no children -- the whole cost is this module's own body)"
    return "\n".join(f"    {k.cumulative_ms:7.1f}ms  {k.name}" for k in kids)


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_warm_reach_entry_point_imports_under_the_ceiling():
    runs = _batch()
    located = [(nodes, _find(nodes, ENTRY_POINT)) for nodes in runs]
    located = [(nodes, i) for nodes, i in located if i >= 0]
    assert located, (
        f"{ENTRY_POINT} never appeared in -X importtime output -- the entry point "
        "moved, or the importtime format changed"
    )
    nodes, index = min(located, key=lambda pair: pair[0][pair[1]].cumulative_ms)
    entry = nodes[index]

    assert entry.cumulative_ms < CEILING_MS, (
        f"warm-reach entry point costs {entry.cumulative_ms:.1f}ms of import CPU "
        f"(min of {_REPEATS}), over AC3's {CEILING_MS}ms ceiling.\n"
        f"Its real children, costliest first:\n{_breakdown(nodes, index)}\n"
        "A heavy stdlib module imported at module scope for one call inside one "
        "function is how this budget goes twice over -- what `asyncio` did here "
        "before it was deferred into `reentrant_dispatch`.\n"
        "ATTRIBUTE FROM THE LIST ABOVE, NOT FROM A SORTED DUMP OF THE WHOLE RUN: "
        "`site` and this entry point are both depth 0, so a flat sort makes an "
        "unrelated sibling look like the dominant child. See this module's "
        "docstring, WHY THE TREE IS KEPT."
    )


def test_the_entry_point_is_not_charged_for_interpreter_startup():
    """`site` is a depth-0 SIBLING of the entry point, never its parent -- so
    whatever `site` costs (editable-install finders, .pth hooks, sitecustomize)
    is not in the number the ceiling above asserts on.

    Pinned because getting this backwards is a one-line mistake with a
    confident conclusion attached: it turns "our import budget regressed" into
    "the box is polluted, not our code", and it was made twice in one session
    before this test existed."""
    nodes = _importtime_run()
    entry_index = _find(nodes, ENTRY_POINT)
    site_index = _find(nodes, "site")
    if site_index < 0:
        pytest.skip("no `site` row -- interpreter started with -S")

    assert nodes[entry_index].depth == 0 and nodes[site_index].depth == 0, (
        "expected `site` and the entry point to both be depth-0 roots; got "
        f"site={nodes[site_index].depth}, entry={nodes[entry_index].depth}"
    )
    subtree = {n.name for n in _children_of(nodes, entry_index)}
    assert "site" not in subtree, (
        "`site` appeared as a CHILD of the entry point -- if that is genuinely "
        "true the ceiling now includes interpreter startup and the budget must "
        "be re-derived, not merely re-run"
    )


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_warm_reach_entry_point_pulls_neither_forbidden_graph():
    reached = {
        node.name
        for run in _batch()
        for node in run
        if node.name.startswith(FORBIDDEN_PREFIXES)
    }
    assert not reached, (
        "the warm-reach entry point pulled forbidden module(s): "
        f"{sorted(reached)}. Importing either graph registers every op via "
        "package __init__ side effects -- the client-side cost this transport "
        "exists to stop paying, and it is paid before any code decides to skip it"
    )
