"""Deterministic sizing floor: counted ask parts map to a minimum t-shirt, with no model call.

Pure functions only. `floor` is the single source of the floor; `sizing-assemble` refuses a
t-shirt below it and prints the same fingerprint for the same input.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping, Optional

from coordinator_core.ops.dispatch_emit.run_truth_contract import TSHIRTS, SizingFingerprint

_PART_RE = re.compile(r"\bP-[A-Z][A-Z0-9]*\b")

# (max parts inclusive, floor t-shirt); more parts than the last bound floor at XL.
_FLOOR_BANDS: tuple[tuple[int, str], ...] = ((1, "XS"), (3, "S"), (6, "M"), (10, "L"))
_TOP_FLOOR = "XL"


def part_ids(intent: Optional[str], register: Optional[Iterable[Mapping[str, str]]] = None) -> tuple[str, ...]:
    """Distinct part ids named in the intent (`P-A`, `P-F17`) and the register rows, sorted."""
    ids = set(_PART_RE.findall(intent or ""))
    for row in register or ():
        rid = str(row.get("id", "")) if isinstance(row, Mapping) else ""
        if _PART_RE.fullmatch(rid):
            ids.add(rid)
    return tuple(sorted(ids))


def floor(intent: Optional[str], register: Optional[Iterable[Mapping[str, str]]] = None) -> SizingFingerprint:
    """The minimum t-shirt for an ask, from its counted parts; an ask naming no part floors at XS."""
    rows = part_ids(intent, register)
    count = len(rows)
    shirt = _TOP_FLOOR
    for bound, name in _FLOOR_BANDS:
        if count <= bound:
            shirt = name
            break
    return SizingFingerprint(parts_count=count, tshirt_floor=shirt, rows=rows)


def below_floor(tshirt: str, fingerprint: SizingFingerprint) -> bool:
    """True when `tshirt` is a known size smaller than the fingerprint's floor."""
    if tshirt not in TSHIRTS:
        return False
    return TSHIRTS.index(tshirt) < TSHIRTS.index(fingerprint.tshirt_floor)
