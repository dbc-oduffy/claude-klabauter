"""Completion gate predicate: why a run's outcome may not be reported complete.

`completion_blockers` is pure (no subprocess, no corpus read). Its last blocker mirrors
`coordinator/bin/run-outcome-audit.py::_judge`, so a run the gate passes is green there.
"""

from __future__ import annotations

from typing import Optional

from coordinator_core.ops.dispatch_emit.ask_fanout_contract import STATUS_HALTED
from coordinator_core.ops.dispatch_emit.halted_run import STATUS_RESUMED
from coordinator_core.ops.dispatch_emit.requirements_ledger import ORIGIN_PM_VERBATIMS

JUDGE_AGENT = "coordinator:exit-criterion-judge"
PM_VERBATIM_ORIGIN = ORIGIN_PM_VERBATIMS

_STANDING = (STATUS_HALTED, STATUS_RESUMED)


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _clause_id(clause) -> str:
    if isinstance(clause, dict):
        return str(clause.get("id") or clause.get("clause") or clause)
    return str(clause)


def completion_blockers(*, inline_review: Optional[dict], halt_marker: Optional[dict]) -> list:
    """Short blocker strings; empty only when the run is complete and judge-backed.

    `halt_marker` is the `halted.json` payload (status `halted`/`resumed` stands); `inline_review` is the
    run-outcome `inline_review` document (`criterion`, `tests`).
    """
    blockers: list = []
    if isinstance(halt_marker, dict) and halt_marker.get("status") in _STANDING:
        reason = halt_marker.get("halted") or halt_marker.get("reason") or halt_marker.get("status")
        blockers.append(f"halt standing: {' '.join(str(reason).split())[:120]}")

    review = inline_review if isinstance(inline_review, dict) else {}
    criterion = review.get("criterion")
    criterion = criterion if isinstance(criterion, dict) else {}

    withheld = criterion.get("withheld_clauses")
    if withheld:
        ids = withheld if isinstance(withheld, (list, tuple, set)) else [withheld]
        blockers.append("withheld clause: " + ", ".join(_clause_id(c) for c in ids))
    if criterion.get("status") != "met":
        blockers.append("criterion not met")
    if criterion.get("authored_by") != JUDGE_AGENT:
        blockers.append("verdict not judge-authored")
    falsifier = criterion.get("falsifier")
    falsifier = falsifier if isinstance(falsifier, dict) else {}
    if not _text(falsifier.get("output")):
        blockers.append("no captured falsifier output")

    tests = review.get("tests")
    tests = tests if isinstance(tests, dict) else {}
    run = tests.get("run")
    tests_ok = tests.get("status") == "pass" and isinstance(run, int) and not isinstance(run, bool) and run > 0
    if not _text(criterion.get("observation")) or not tests_ok:
        blockers.append("not backed")
    return blockers
