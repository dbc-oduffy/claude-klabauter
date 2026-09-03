"""
coordinator_core.workstream_complete.test_terminal_sweep_directive_ordering
— pins the one property neither the generic cross-consistency guard nor any
existing test asserted: the terminal-handoff and terminal-sizings sweep
directives are emitted LAST, in that order, from `build_directives`.

Purpose: the C3 commit that added `d-sweep-terminal-sizings`
(docs/plans/2026-09-03-close-verb-archival-stops-asking-for-wri.md) calls
ordering load-bearing in its own commit message ("every stamp this
ceremony performed must land before the sweep classifies") but shipped no
test asserting it. `coordinator_core/authz/tests/test_assembler_
dispatchable.py`'s cross-consistency guard only catches a CLI present in
`CONSUMES_MANIFEST` but missing from `ASSEMBLER_DISPATCHABLE` (or the
reverse) — it says nothing about WHERE in the list a directive lands. This
gap predates the sizings sweep: `d-sweep-terminal-handoffs` was never
order-pinned either, so this file closes both at once rather than only the
newer one.

Negative-spec: does NOT assert anything about the OTHER directives'
ordering, relative positions, or presence/absence — only that the two
terminal sweeps are the final two entries, handoffs immediately before
sizings.
"""

from __future__ import annotations

from pathlib import Path

import coordinator_core.workstream_complete as wsc
from coordinator_core.ops.ceremony.wsc_disposition import SINGLE_SESSION


def _gate() -> wsc.SessionShapeGate:
    return wsc.SessionShapeGate(
        sid="testsid-terminal-sweep-order",
        disposition=SINGLE_SESSION,
        consumed_handoff="",
        diagnostics=[],
        consumed_handoff_paths=(),
    )


def test_terminal_sweeps_are_last_handoffs_then_sizings(tmp_path: Path) -> None:
    directives = wsc.build_directives(_gate(), {}, tmp_path)

    ids = [d["id"] for d in directives]
    assert ids[-2:] == ["d-sweep-terminal-handoffs", "d-sweep-terminal-sizings"], (
        f"expected the handoffs sweep immediately followed by the sizings "
        f"sweep as the final two directives; got {ids!r}"
    )

    handoffs = directives[-2]
    sizings = directives[-1]
    assert handoffs["cli"] == "sweep-terminal-handoffs"
    assert sizings["cli"] == "sweep-terminal-sizings"
