"""Premise-repair interface: repair kinds, the premise-checker's ``corrected:`` citation parse,
and the execution-time falsifier row builder. Pure stdlib, no IO.

Contract source: premise-checker.md § Verdicts (``corrected: <citation>`` in a row's evidence;
``convertible: execution-time falsifier row`` on an UNCHECKABLE row).
"""

from __future__ import annotations

import re
from typing import Any, Optional

REPAIR_CITATION = "premise-citation-corrected"
REPAIR_FALSIFIER = "uncheckable-to-falsifier-row"
REPAIR_REGISTER_PIN = "register-pin-missing"

_CORRECTED = re.compile(r"(?i)\bcorrected\s*:\s*(?:`([^`]+)`|(\S+))")
_TRAILING = ".,;:)]}\"'"


def corrected_citation(row: Any) -> Optional[str]:
    """The citation after ``corrected:`` in ``row["evidence"]``, or None when the row carries none.

    Backticks and trailing sentence punctuation are stripped; an empty result is None.
    """
    if not isinstance(row, dict):
        return None
    evidence = row.get("evidence")
    if not isinstance(evidence, str):
        return None
    match = _CORRECTED.search(evidence)
    if match is None:
        return None
    value = (match.group(1) or match.group(2) or "").strip().rstrip(_TRAILING).strip()
    return value or None


def convertible(row: Any) -> bool:
    """True for an UNCHECKABLE row; the ``convertible:`` marker is optional, never required."""
    return isinstance(row, dict) and row.get("verdict") == "UNCHECKABLE"


def falsifier_task(anchor_id: str, claim: str, next_id: str) -> dict:
    """A plan-tasks row that restates ``claim`` as an execution-time check.

    ``performer: em`` is load-bearing: an executor row that writes nothing and runs a check is
    refused by prep_gate's test-runner-out-of-role-row finding.
    """
    claim = " ".join(str(claim).split())
    return {
        "id": next_id,
        "title": f"Execution-time falsifier for premise {anchor_id}",
        "change_kind": "test-edit",
        "performer": "em",
        "writes": [],
        "reads_at_head": [],
        "consumes": [],
        "acceptance": f"At execution time, verify premise {anchor_id} holds: {claim}",
        "queue_scope": "project",
        "disposition": "open",
        "body": (
            f"Converted from UNCHECKABLE premise {anchor_id}. Check the claim when the plan "
            f"executes: {claim}. If it does not hold, stop and report the plan's premise as "
            "falsified."
        ),
    }
