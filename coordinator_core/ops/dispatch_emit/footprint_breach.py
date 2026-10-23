"""Landing-time serving writes and the met-on-breach judgement for a ledger resume.

Pure: no git, no subprocess. Trap: an unreadable or absent plan yields `[]` from `serving_writes`,
so the footprint fence stays strict (falls back to the recorded artifact) rather than widening.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Final, Mapping, Optional, Sequence

MET_ON_FOOTPRINT_BREACH_REASON: Final[str] = "met rests on a path withheld as outside-row-footprint"


def serving_writes(plan_abs: Optional[Path], blocking: Sequence[str]) -> list[str]:
    """Ordered, de-duplicated `writes` of every plan row serving a blocking id, read now."""
    if plan_abs is None:
        return []
    from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import (
        PlanRowsUnreadable,
        plan_rows_serving,
    )

    try:
        return plan_rows_serving(Path(plan_abs), blocking)
    except PlanRowsUnreadable:
        return []


def _names(text: str, token: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", text) is not None


def met_on_breach(
    items: Sequence[object], breach: Sequence[str], row_writes: Mapping[str, Sequence[str]],
) -> list[dict]:
    """`{id, reason}` per `met` verdict item resting on a breach path, in verdict order.

    An item rests on breach path p when p, its file name, or the id of a row whose `row_writes`
    contain p appears as a whole token in the item's `met_by` or `evidence` text.
    """
    if not breach:
        return []
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict) or item.get("status") != "met":
            continue
        met_by = item.get("met_by")
        met_text = " ".join(str(m) for m in met_by) if isinstance(met_by, (list, tuple)) else str(met_by or "")
        text = f"{met_text} {item.get('evidence') or ''}"
        hit = [
            p for p in breach
            if _names(text, p)
            or _names(text, p.rsplit("/", 1)[-1])
            or any(p in (w or ()) and _names(text, str(rid)) for rid, w in row_writes.items())
        ]
        if hit:
            out.append({
                "id": item.get("id"),
                "reason": f"{MET_ON_FOOTPRINT_BREACH_REASON}: {', '.join(hit)}",
            })
    return out
