"""Plan-size bounds and their measurement; the one home prep_gate's PLAN_SIZE class consumes.

Stdlib only, and prep_gate imports this module, never the reverse.

Calibration of the literals (research 12b979 FINDINGS 59 / R11 is why the bounds exist):

* Row counts L 6 / XL 8 / XXL 10 come from the census in
  docs/plans/2026-10-23-smaller-plans-at-xl.md frontmatter: spine rows > 6, > 8, > 10 across
  the 449 plans dated 2026-10-2x were 94, 53 and 25 plans.
* The 3000-byte row-body bound sits in the upper half of the original 18-row sample
  (1231-5019 bytes, median about 2500) and is deliberately aggressive.
* Re-measured at C0 time over three plans, 18 rows (the original three paths were not
  recorded, so the exact sample did not reproduce): 489-6231 bytes, median 1851, from
    docs/plans/2026-10-23-051a3d-2-post-exec-review-verify-tail.md
    docs/plans/2026-10-23-2026-10-23-cause2-in-route-halt-repair.md
    docs/plans/2026-10-23-seam-test-failure-repairs-in-route.md
  Measuring one-liner (repo root):
    PYTHONPATH=. python3 -c "import sys,statistics as s;from coordinator_core.roadmap.prep_gate import raw_spine_rows as r;b=[len((x.get('body') or '').encode()) for p in sys.argv[1:] for x in r(open(p).read())];print(len(b),min(b),max(b),s.median(b))" <plan paths>
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Tuple

BOUNDS: Mapping[str, Tuple[int, int]] = {
    "L": (6, 3000),
    "XL": (8, 3000),
    "XXL": (10, 3000),
}

# Mirrors prep_gate._UNSCHEDULABLE_DISPOSITIONS (not importable: prep_gate imports this module).
_CLOSED_DISPOSITIONS = frozenset(
    {"coded", "done", "spun_off", "backlogged", "wont_do", "voided", "delivered_elsewhere"}
)


def bounds_for(tshirt: Any) -> Optional[Tuple[int, int]]:
    """(max_rows, max_row_body_bytes) for a tshirt, case-insensitive; None when unbounded."""
    if not isinstance(tshirt, str):
        return None
    return BOUNDS.get(tshirt.strip().upper())


@dataclass(frozen=True)
class PlanSize:
    """Measured size of a plan's open spine rows."""

    rows: int
    row_body_bytes: Tuple[Tuple[str, int], ...]

    def over(self, bounds: Tuple[int, int], tshirt: str = "") -> list[str]:
        """One line per exceeded bound, e.g. "7 rows > 6 at L", "C3 body 4120 bytes > 3000"."""
        max_rows, max_bytes = bounds
        out: list[str] = []
        if self.rows > max_rows:
            at = f" at {tshirt.strip().upper()}" if tshirt.strip() else ""
            out.append(f"{self.rows} rows > {max_rows}{at}")
        for row_id, size in self.row_body_bytes:
            if size > max_bytes:
                out.append(f"{row_id} body {size} bytes > {max_bytes}")
        return out


def _is_open_and_scheduled(row: Mapping[str, Any]) -> bool:
    if row.get("deferred") is True or bool(row.get("deferred_until")):
        return False
    return str(row.get("disposition") or "").strip() not in _CLOSED_DISPOSITIONS


def measure(rows: Iterable[Any]) -> PlanSize:
    """Count open, unheld rows and the UTF-8 byte length of each one's body (absent = 0)."""
    sizes: list[Tuple[str, int]] = []
    for row in rows:
        if not isinstance(row, Mapping) or not _is_open_and_scheduled(row):
            continue
        body = row.get("body")
        size = len(body.encode("utf-8")) if isinstance(body, str) else 0
        sizes.append((str(row.get("id") or "<row with no id>"), size))
    return PlanSize(rows=len(sizes), row_body_bytes=tuple(sizes))
