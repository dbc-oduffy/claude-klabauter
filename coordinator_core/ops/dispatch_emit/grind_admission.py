"""Pure grind admission arithmetic: how many selected rows a run's budget covers.

Charge = RUN_FIXED + ceil(rows / batch_size) * BATCH + ceil(rows * ROW_CENTI / 100). Triage, the
batched verify-routing kinds (route, retire, hand-back), close and adjudicate run once per batch;
fix, its verify-agent and the review wave run per row that needs a code fix.
Calibrated on claude-klabauter 20261114T173532Z (batch 40, verify-routing batched, b2ed825d22): 10 rows,
17 calls = run 2 (commit, ledger-sweep) + batch 6 (triage 1, verify-routing 4, close 1; adjudicate
reserved, +1) + row 9 (fix 3, verify-agent 2, review 4), 795k subagent tokens, 9 rows landed.
The unbatched receipts (20261114T165441Z: 23 verify calls for 10 rows) predate b2ed825d22.
"""

from __future__ import annotations

from typing import NamedTuple, Optional, Sequence

ROW_AGENT_CALLS_CENTI = 90
BATCH_AGENT_CALLS = 7
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
