"""Served-op coverage over a traffic manifest, and the KR4 ratio that refuses without the resident leg.

Reads a manifest as a plain dict (shape pinned in the invocation-traffic-manifest spike plan,
§ Manifest contract); never loads the schema. Registry imports stay lazy so importing this
module does not pull the op registry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional

KR4_REFUSAL_REASON = "resident_bash leg pending (B-β)"

_DEFAULT_INVENTORY = Path(__file__).resolve().parents[2] / ".github" / "op-inventory.json"


def served_ops() -> FrozenSet[str]:
    """Op keys in OP_MODULE_MAP at the running HEAD."""
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    return frozenset(OP_MODULE_MAP)


def _inventory_op_keys(path: Path) -> FrozenSet[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return frozenset(row["op_key"] for row in data)


def reconcile(inventory_path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Set differences between served_ops() and each other op table, by table name.

    Each entry: {"served_not_in_table": [...], "table_not_served": [...], "table_size": int}.
    Reports only; never asserts the tables equal.
    """
    from coordinator_core.authz.classification import OP_CLASSIFICATION
    from coordinator_core.op_scopes import _OP_KEY_SCOPE

    served = served_ops()
    tables = {
        "op-inventory.json": _inventory_op_keys(inventory_path or _DEFAULT_INVENTORY),
        "OP_CLASSIFICATION": frozenset(OP_CLASSIFICATION),
        "_OP_KEY_SCOPE": frozenset(_OP_KEY_SCOPE),
    }
    return {
        name: {
            "served_not_in_table": sorted(served - keys),
            "table_not_served": sorted(keys - served),
            "table_size": len(keys),
        }
        for name, keys in tables.items()
    }


def coverage(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Engine-served numerator, unserved split, and kr4_ratio.

    kr4_ratio is None with kr4_reason unless legs.resident_bash.status == "measured"; then
    numerator / (engine total + resident total).
    """
    served = served_ops()
    legs = manifest["legs"]
    engine = legs["engine_served"]
    numerator = 0
    unserved = []
    for op, row in engine["ops"].items():
        count = row["count"]
        if op in served:
            numerator += count
        else:
            unserved.append({"op": op, "count": count})
    unserved.sort(key=lambda r: (-r["count"], r["op"]))

    resident = legs["resident_bash"]
    ratio: Optional[float] = None
    reason: Optional[str] = KR4_REFUSAL_REASON
    if resident.get("status") == "measured":
        total = engine["total"] + resident["total"]
        reason = None if total else "zero total traffic"
        ratio = numerator / total if total else None
    return {
        "served_count": numerator,
        "unserved": unserved,
        "kr4_ratio": ratio,
        "kr4_reason": reason,
    }
