"""Pure pre-rule for the verify leg's ledger items: engine-known fields only, no judgment.

Stdlib plus `ledger_remaining_paths._clean`; no git, no subprocess. Field names mirror
`ask_compose._LEDGER_CHECK_SCHEMA["items"]`. `met` is never set here: that is the verify agent's call.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import _clean

#: Register statuses the engine copies verbatim; every other status is the verify agent's.
PRERULED_STATUSES = ("deferred", "waived")


def _norm(path: object) -> str:
    return _clean(str(path)) or str(path).strip().replace("\\", "/")


def _withheld_index(withheld_rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in withheld_rows or ():
        for rid in row.get("register_rows") or ():
            out.setdefault(str(rid), str(row["id"]))
    return out


def prerule(
    items: Sequence[Mapping[str, Any]],
    landed: Mapping[str, str],
    withheld_rows: Iterable[Mapping[str, Any]],
    register_status: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Return ledger items with the engine-known fields filled.

    Each input item carries `id` and optional `paths` (its declared write paths). `landed` maps a
    landed path to its sha; `withheld_rows` are manifest rows (`id`, `register_rows`).
    Sets `withheld_by` (the withheld chunk id), `remaining` (declared paths not in `landed`),
    `met_by` (shas of the declared paths that landed, in declared order, deduplicated) and
    `status` only when the register already holds `deferred` or `waived`. Never sets `met`.
    """
    held = _withheld_index(withheld_rows)
    landed_n = {_norm(p): str(s) for p, s in (landed or {}).items()}
    out: list[dict[str, Any]] = []
    for item in items:
        rid = str(item["id"])
        paths = [_norm(p) for p in item.get("paths") or ()]
        row: dict[str, Any] = {"id": rid}
        if rid in held:
            row["withheld_by"] = held[rid]
        remaining = [p for p in dict.fromkeys(paths) if p not in landed_n]
        if remaining:
            row["remaining"] = remaining
        shas = list(dict.fromkeys(landed_n[p] for p in paths if p in landed_n))
        if shas:
            row["met_by"] = shas
        status = register_status.get(rid)
        if status in PRERULED_STATUSES:
            row["status"] = status
        out.append(row)
    return out


def fully_decided(items: Sequence[Mapping[str, Any]]) -> bool:
    """True when every item carries a status (and there is at least one item)."""
    return bool(items) and all(i.get("status") for i in items)
