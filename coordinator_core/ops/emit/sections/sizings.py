"""Parity oracle: none (net-new section; covered by tests/test_sizings_and_workflow_runs_sections.py).

Section porter — SizingSummary (envelope key: ``sizings``; served by ``sizings.summary``).

One row per ``state/sizings/*.yaml``, read in-process through the ceremony ``sizing-object``
record query (its content-keyed parse memo and per-worktree record cache). Emits nothing
while the vendored schema pin lacks ``sizing-summary.schema.json``, so emit never fails
validation before the pin is re-vendored. A sizing missing a required field is returned in
the malformed list, which ``sizings.summary`` reports as ``{path, reason}``.
"""

from __future__ import annotations

import re

from pydantic import ValidationError

from coordinator_core.contract.cockpit_schema.entities.sizing_summary import SizingSummary
from coordinator_core.ops.ceremony.records_query import query_records
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.validate import _entity_schema_path

ENTITY = "sizing-summary"
_CLOSED_STATUSES = frozenset({"closed", "archived"})
_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})-")


def _resolution(status: str, linked_plan: str | None) -> str:
    if linked_plan:
        return "planned"
    if status in _CLOSED_STATUSES:
        return "closed"
    return "unresolved"


def _nonempty_str(v: object) -> str | None:
    return v if isinstance(v, str) and v else None


def _project(path: str, fm: dict) -> dict:
    estimate = fm.get("estimate")
    exit_criterion = fm.get("exit_criterion")
    accepted = exit_criterion.get("accepted") if isinstance(exit_criterion, dict) else None
    status = fm.get("status")
    plan = _nonempty_str(fm.get("plan"))
    m = _DATE_PREFIX.match(path.rsplit("/", 1)[-1])
    return {
        "path": path,
        "intent": fm.get("intent"),
        "route": fm.get("route"),
        "tshirt": estimate.get("tshirt") if isinstance(estimate, dict) else None,
        "status": status,
        "xl_exit": _nonempty_str(fm.get("xl_exit")),
        "created": m.group(1) if m else None,
        "exit_criterion_accepted": bool(accepted),
        "linked_plan": plan,
        "resolution": _resolution(status if isinstance(status, str) else "", plan),
    }


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    if not _entity_schema_path(ENTITY).exists():
        return [], []
    root = ctx.subprocess_root if ctx.subprocess_root is not None else ctx.repo_root
    try:
        raw = query_records("sizing-object", root, limit=0)
    except (ValueError, SystemExit, OSError):
        return [], []

    records: list[dict] = []
    malformed: list[dict] = []
    for rec in raw if isinstance(raw, list) else []:
        fm = rec.get("frontmatter") if isinstance(rec, dict) else None
        if not isinstance(fm, dict):
            continue
        path = rec["path"]
        row = _project(path, fm)
        try:
            SizingSummary.model_validate(row)
        except ValidationError as exc:
            malformed.append(
                {
                    "path": path,
                    "reason": f"fails SizingSummary: {exc.error_count()} violation(s)",
                    "frontmatter_keys": sorted(fm.keys()),
                }
            )
            continue
        records.append(row)
    return records, malformed
