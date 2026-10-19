"""coordinator_core.ops.dispatch_emit.inventory_accounting -- the per-item ledger
that proves every Chunk-table row of an inventory landed in exactly one of
emitted, skipped-with-reason or refused.

Purpose: `account` is the invariant (a gap or a double entry raises),
`merge_parts` unions the accountings of the parts of one split emit, and
`summary_lines` renders the CLI text including one line per plan file the
emit edited. Pure functions over ids already parsed by the caller; no I/O.

Buckets:
  emitted  -- at least one of the item's minted rows is in the composed waves.
  skipped  -- the item mints nothing now by the inventory's or plan's own state.
  refused  -- an emit gate dropped the item's rows from dispatch.

Negative-spec:
  - Does NOT warn on a gap: an unaccounted or doubly accounted id raises.
  - Does NOT read files or spawn processes.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Sequence, Union

IN_ANOTHER_PART = "in another part"

_Reasons = Union[Mapping[str, str], Iterable[Mapping[str, str]]]


class InventoryAccountingError(ValueError):
    """Some inventory item is unaccounted for, in two buckets, or not in the inventory."""


def _pairs(entries: _Reasons) -> List[tuple]:
    if isinstance(entries, Mapping):
        return [(str(k), str(v)) for k, v in entries.items()]
    return [(str(e["id"]), str(e.get("reason", ""))) for e in entries]


def account(
    item_ids: Sequence[str],
    emitted: Iterable[str],
    skipped: _Reasons,
    refused: _Reasons,
) -> Dict[str, Any]:
    """Bucket every inventory item; raise `InventoryAccountingError` naming each
    id that is unaccounted, in two buckets, or not an inventory item.

    `skipped` and `refused` are `{id: reason}` or a list of `{"id", "reason"}`.
    Returns the reply shape in table order."""
    ordered = list(dict.fromkeys(str(i) for i in item_ids))
    known = set(ordered)
    emitted_ids = list(dict.fromkeys(str(i) for i in emitted))
    skipped_pairs = _pairs(skipped)
    refused_pairs = _pairs(refused)

    seen: Dict[str, int] = {}
    for bucket in (emitted_ids, [i for i, _ in skipped_pairs], [i for i, _ in refused_pairs]):
        for i in bucket:
            seen[i] = seen.get(i, 0) + 1
    unaccounted = [i for i in ordered if i not in seen]
    doubled = [i for i in ordered if seen.get(i, 0) > 1]
    stray = sorted(i for i in seen if i not in known)
    if unaccounted or doubled or stray:
        parts = []
        if unaccounted:
            parts.append(f"unaccounted: {', '.join(unaccounted)}")
        if doubled:
            parts.append(f"in two buckets: {', '.join(doubled)}")
        if stray:
            parts.append(f"not inventory items: {', '.join(stray)}")
        raise InventoryAccountingError("inventory accounting gap -- " + "; ".join(parts))

    emitted_set = set(emitted_ids)
    skip_reason = dict(skipped_pairs)
    refuse_reason = dict(refused_pairs)
    skipped_out = [{"id": i, "reason": skip_reason[i]} for i in ordered if i in skip_reason]
    refused_out = [{"id": i, "reason": refuse_reason[i]} for i in ordered if i in refuse_reason]
    emitted_out = [i for i in ordered if i in emitted_set]
    return {
        "inventory_rows": len(ordered),
        "emitted": emitted_out,
        "skipped": skipped_out,
        "refused": refused_out,
        "counts": {
            "emitted": len(emitted_out),
            "skipped": len(skipped_out),
            "refused": len(refused_out),
        },
    }


def merge_parts(accountings: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Union the accountings of one inventory's parts into one.

    An item emitted in any part is emitted; a refusal beats a skip; a real skip
    beats "in another part". An item whose only entry is "in another part" has
    no sibling that took it and raises."""
    ordered: List[str] = []
    emitted: set = set()
    refused: Dict[str, str] = {}
    skipped: Dict[str, str] = {}
    elsewhere: set = set()
    for acc in accountings:
        for i in acc.get("emitted", []):
            if i not in ordered:
                ordered.append(i)
            emitted.add(i)
        for bucket, target in (("skipped", skipped), ("refused", refused)):
            for entry in acc.get(bucket, []):
                i = entry["id"]
                if i not in ordered:
                    ordered.append(i)
                if bucket == "skipped" and entry["reason"] == IN_ANOTHER_PART:
                    elsewhere.add(i)
                else:
                    target.setdefault(i, entry["reason"])
    skipped_final: Dict[str, str] = {}
    for i in ordered:
        if i in emitted or i in refused:
            continue
        if i in skipped:
            skipped_final[i] = skipped[i]
    refused_final = {i: r for i, r in refused.items() if i not in emitted}
    # An id only ever "in another part" is left out of every bucket so account() names it.
    return account(ordered, emitted, skipped_final, refused_final)


def summary_lines(
    accounting: Mapping[str, Any], landed_reconciled: Mapping[str, Sequence[str]]
) -> List[str]:
    """CLI text: one count line, one line per skipped/refused id with its reason,
    one `edited <plan path>: <row ids> -> coded` line per plan file edited."""
    counts = accounting["counts"]
    lines = [
        f"inventory accounting: {accounting['inventory_rows']} rows = "
        f"{counts['emitted']} emitted + {counts['skipped']} skipped + {counts['refused']} refused"
    ]
    for entry in accounting["skipped"]:
        lines.append(f"  skipped {entry['id']}: {entry['reason']}")
    for entry in accounting["refused"]:
        lines.append(f"  refused {entry['id']}: {entry['reason']}")
    for plan, rows in landed_reconciled.items():
        lines.append(f"edited {plan}: {', '.join(rows)} -> coded")
    return lines
