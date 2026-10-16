"""Cockpit-required frontmatter rule, one definition for the emit sections and the write-time guards.

Stdlib-only leaf: importing it must not pull `ops.records_query` onto a Write hook.
"""

from __future__ import annotations

# Handoff fields the cockpit contract needs as strings.
REQUIRED_STRING_FIELDS = ("title", "created", "status", "deployment_state")

# PlanStatus enum, in parity with coordinator_core/frontmatter/schemas/plan.schema.json's `status`
# enum. Order-insensitive membership set.
PLAN_STATUS_ENUM = frozenset({
    "draft",
    "reviewed",
    "approved",
    "blocked",
    "executing",
    "landed",
    "implemented",
    "closed_partial",
    "deferred",
    "abandoned",
    "superseded",
})


def missing_required_fields(fm: dict) -> list[str]:
    """Names of the frontmatter fields that make a plan fail the emit filter; [] when valid.
    A status outside PlanStatus is reported as `status`."""
    missing = [f for f in ("title", "created", "author") if not isinstance(fm.get(f), str)]
    status = fm.get("status")
    if not isinstance(status, str) or status not in PLAN_STATUS_ENUM:
        missing.append("status")
    return missing
