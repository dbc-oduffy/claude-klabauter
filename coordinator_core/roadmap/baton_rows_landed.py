"""Pure reader: has every row of a baton's plan spine landed, and under which sha."""

from __future__ import annotations

from typing import Optional

from coordinator_core.frontmatter.body_blocks import LocateStatus
from coordinator_core.ops.dispatch_emit.plan_row_coding import row_rank
from coordinator_core.ops.dispatch_emit.spine_read import (
    NON_DISPATCHABLE_DISPOSITIONS,
    load_rows_memo,
    with_canonical_disposition,
)


def rows_landed(plan_text: str) -> Optional[dict]:
    """``{"coded_sha": <sha or None>, "rows": [coded row ids]}`` when every spine row is in a
    NON_DISPATCHABLE disposition and at least one is coded; ``None`` for an unlocated,
    unparseable, empty or not-fully-landed spine. ``coded_sha`` is the ``disposition_ref`` of
    the last coded row (by ``row_rank``, then spine order) that carries one. No I/O."""
    result = load_rows_memo(plan_text)
    if result.status is not LocateStatus.LOCATED:
        return None
    rows = [with_canonical_disposition(r) for r in result.rows]
    if not rows or not all(isinstance(r, dict) and r.get("id") for r in rows):
        return None
    if any(r.get("disposition") not in NON_DISPATCHABLE_DISPOSITIONS for r in rows):
        return None
    coded = sorted(
        (r for r in rows if r.get("disposition") == "coded"), key=row_rank
    )
    if not coded:
        return None
    shas = [str(r["disposition_ref"]) for r in coded if r.get("disposition_ref")]
    return {
        "coded_sha": shas[-1] if shas else None,
        "rows": [str(r["id"]) for r in coded],
    }
