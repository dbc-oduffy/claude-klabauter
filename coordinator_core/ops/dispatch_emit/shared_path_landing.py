"""Shared-path landing: a met row's product path that a live sibling holds, and the refusal naming it.

``land_run`` builds ``(row, path, reason)`` triples for every credited row path it leaves out; the
helpers here join the ``peer-held`` ones to the ``{path: [holders]}`` map and word the refusal. No
spawn. Stdlib only at module top (terminal_commit cold-import pin); readers are imported lazily.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Optional

#: ``refused`` value of a landing whose met row's product path a live sibling holds.
SHARED_PATH_SIBLING_HELD = "shared-path-sibling-held"

#: Reply key: sorted unique paths a landing left out that a credited row declared.
LEDGER_WITHHELD_PRODUCT = "ledger_withheld_product"

_PEER_HELD = "peer-held"


def sibling_held_product(unlanded: Iterable, peer_held: Mapping) -> list[tuple[str, str, list[str]]]:
    """Sorted ``(row, path, holders)`` for each ``peer-held`` unlanded triple, holders from ``peer_held``.

    A triple whose path ``peer_held`` does not name carries empty holders. A malformed triple raises:
    this gate refuses a landing, so a shape bug must surface rather than let tests land without product."""
    out: set[tuple[str, str, tuple[str, ...]]] = set()
    for row, path, reason in unlanded or ():
        if reason != _PEER_HELD:
            continue
        holders = tuple(sorted(str(h) for h in (peer_held or {}).get(path, ()) or ()))
        out.add((str(row), str(path), holders))
    return [(row, path, list(holders)) for row, path, holders in sorted(out)]


def withheld_product_paths(unlanded: Iterable) -> list[str]:
    """Sorted unique paths of every unlanded ``(row, path, reason)`` triple; never raises."""
    try:
        return sorted({str(path) for _row, path, _reason in unlanded or ()})
    except Exception:  # noqa: BLE001
        return []


def refusal_message(holds: Iterable, run_id: str, root: Optional[Path] = None) -> str:
    """One register line naming each held path, its row and holder(s), then the route.

    Route one: wait for the holder to land, then re-fire (the run's refire command when ``root``
    resolves its sizing). Route two: leave the paths out via ``peer_claim_contract.WITHHOLD_CALL``."""
    from coordinator_core.ops.dispatch_emit import peer_claim_contract

    named = "; ".join(
        f"{path} (row {row}) held by {', '.join(holders) or 'a live sibling'}"
        for row, path, holders in sorted(holds)
    )
    wait = "Wait for the holder to land, then re-fire."
    sizing = ""
    if root is not None:
        try:
            from coordinator_core.ops import warp_run_deliverable

            sizing = warp_run_deliverable.record_sizing_rel(Path(root), run_id)
        except Exception:  # noqa: BLE001 -- no resolvable sizing: the generic wait route stands
            sizing = ""
    if sizing:
        from coordinator_core.ops.dispatch_emit import sibling_claims

        wait = f"Wait for the holder to land, then {sibling_claims.reland_remedy(sizing)}"
    withhold = peer_claim_contract.WITHHOLD_CALL.format(run_id=run_id)
    return (
        f"{SHARED_PATH_SIBLING_HELD}: {named}. A met row's product path lands with its tests or not at all. "
        f"{wait} Or leave them out: {withhold}"
    )
