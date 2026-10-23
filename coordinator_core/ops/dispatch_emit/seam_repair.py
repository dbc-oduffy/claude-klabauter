"""Seam-repair interface: the agent label, reply schema, prompt tail and repairable-class test.

Pure: no I/O, no git. `emit.py` renders `REPAIRABLE_CLASSES` into the workflow JS; the JS
`_SEAM_REPAIRABLE` check is the production gate and `is_repairable` is its Python statement.
"""
from __future__ import annotations

from typing import Iterable

SEAM_FIX_LABEL = "seam-fix:wave-"

#: Finding classes a repair round may act on.
REPAIRABLE_CLASSES = ("execution-failed",)

SEAM_FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["DONE", "BLOCKED"]},
        "paths": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["status", "paths", "reason"],
    "additionalProperties": False,
}


def seam_fix_prompt(tests_failed_output_var: str, footprint_var: str) -> str:
    """Instruction tail for the repair agent; the two args are JS expressions spliced in."""
    return (
        "The wave's seam tests failed. Failing output: ${" + tests_failed_output_var + "}. "
        "The ONLY paths you may edit: ${" + footprint_var + "}. "
        "Reproduce first with `python -m pytest <failing> -q`. "
        "If the test passes on re-run, it is a flake: return DONE with `paths: []`. "
        "Otherwise fix within the footprint and return DONE with every path written. "
        "Return BLOCKED with the reason when the fix needs a path outside the footprint "
        "or the failure is not caused by the wave's change. "
        "Commit nothing. Never edit a test to delete its assertion. "
        "Save captures under `scratch/seam/<run-id>/`."
    )


def is_repairable(findings: Iterable) -> bool:
    """True iff there is a blocking finding and every blocking finding is repairable."""
    blocking = [f for f in (findings or ()) if f and f.get("blocking") is not False]
    return bool(blocking) and all(f.get("class") in REPAIRABLE_CLASSES for f in blocking)
