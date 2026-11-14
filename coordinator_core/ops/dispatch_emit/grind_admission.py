"""Pure grind admission arithmetic: how many selected rows a run's budget covers.

Charge = RUN_FIXED + ceil(rows / batch_size) * BATCH + ceil(rows * ROW_CENTI / 100). Triage, fix,
adjudicate, close and the op verify run once per batch; verify-routing runs once per row (more on
retry). Each constant is the worst measured value across lessons-run receipts:
- claude-klabauter 20261114T165441Z, batch 40, 10 rows in 1 batch: 31 calls = run 2 (commit,
  ledger-sweep) + batch 6 (triage 1, fix 1, adjudicate 1, close 2, verify-op 1) + row 23
  (verify-routing 13, retire 7, hand-back 3; every row retried).
- example-stats-repo 20261113T035948Z, batch 4: 27 verify calls for 12 resolved rows (2.25 per row).
- claude-klabauter 20261113T030828Z, batch 4: 19 verify calls for 16 rows (1.19 per row).
Per-row verify dominates at any batch size: a batch-40 run is about 100 calls, so batching
triage does not bring admission near the per-row floor; batching verify would.
"""

from __future__ import annotations

from typing import NamedTuple, Optional, Sequence

ROW_AGENT_CALLS_CENTI = 230
BATCH_AGENT_CALLS = 6
RUN_FIXED_AGENT_CALLS = 2
DEFAULT_BATCH_SIZE = 4
ROW_OUTPUT_TOKENS = 140076
NOT_ADMITTED_EXTRA_KEY = "not_admitted"


class Admission(NamedTuple):
    admitted: tuple[str, ...]
    not_admitted: tuple[str, ...]
    arithmetic: dict


def admit(
    row_ids_in_run_order: Sequence[str],
    *,
    max_agent_calls: Optional[int],
    budget_tokens: Optional[int],
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Admission:
    """Admit the longest run-order prefix both bounds cover; a None bound is unbounded.

    Raises ValueError on a non-positive bound rather than admitting zero rows silently.
    """
    for name, value in (("max_agent_calls", max_agent_calls), ("budget_tokens", budget_tokens)):
        if value is not None and value <= 0:
            raise ValueError(f"{name} must be positive, got {value}")
    rows = tuple(row_ids_in_run_order)
    if batch_size <= 0:
        raise ValueError(f"batch_size must be positive, got {batch_size}")
    by_calls = None if max_agent_calls is None else _rows_within(max_agent_calls, batch_size, len(rows))
    by_tokens = None if budget_tokens is None else budget_tokens // ROW_OUTPUT_TOKENS
    count = len(rows)
    for bound in (by_calls, by_tokens):
        if bound is not None:
            count = min(count, bound)
    return Admission(
        admitted=rows[:count],
        not_admitted=rows[count:],
        arithmetic={
            "max_agent_calls": max_agent_calls,
            "budget_tokens": budget_tokens,
            "row_agent_calls_centi": ROW_AGENT_CALLS_CENTI,
            "run_fixed_agent_calls": RUN_FIXED_AGENT_CALLS,
            "batch_agent_calls": BATCH_AGENT_CALLS,
            "batch_size": batch_size,
            "row_output_tokens": ROW_OUTPUT_TOKENS,
            "bound_by_calls": by_calls,
            "bound_by_tokens": by_tokens,
            "admitted_count": count,
        },
    )


def run_agent_calls(rows: int, batch_size: int) -> int:
    """Worst-measured agent calls for `rows` rows packed `batch_size` to a batch."""
    if rows <= 0:
        return RUN_FIXED_AGENT_CALLS
    batches = -(-rows // batch_size)
    return RUN_FIXED_AGENT_CALLS + batches * BATCH_AGENT_CALLS + -(-rows * ROW_AGENT_CALLS_CENTI // 100)


def _rows_within(max_agent_calls: int, batch_size: int, available: int) -> int:
    """Largest row count (at most `available`) whose charge fits `max_agent_calls`; cost is monotone."""
    n = 0
    while n < available and run_agent_calls(n + 1, batch_size) <= max_agent_calls:
        n += 1
    return n


def receipt_block(admission: Admission) -> dict:
    """The receipt_extras shape naming the rows a run did not admit."""
    return {
        "count": len(admission.not_admitted),
        "row_ids": list(admission.not_admitted),
        "arithmetic": dict(admission.arithmetic),
    }


def not_admitted_entries(admission: Admission) -> list[dict]:
    """One `{row_id, reason}` per not-admitted row, the reason naming the cap that cut it."""
    caps = []
    if admission.arithmetic.get("bound_by_calls") is not None:
        caps.append(f"max_agent_calls={admission.arithmetic['max_agent_calls']}")
    if admission.arithmetic.get("bound_by_tokens") is not None:
        caps.append(f"budget_tokens={admission.arithmetic['budget_tokens']}")
    count = admission.arithmetic["admitted_count"]
    reason = f"not-admitted: run budget cap ({', '.join(caps)}) covers {count} rows"
    return [{"row_id": row_id, "reason": reason} for row_id in admission.not_admitted]


def claimed_entries(claimed: Sequence) -> list[dict]:
    """One `{row_id, reason}` per row skipped for a live peer row claim."""
    return [
        {"row_id": c.row_id, "reason": f"not-admitted: claimed by live session {c.holder_session}"}
        for c in claimed
    ]
