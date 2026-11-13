"""Pure grind admission arithmetic: how many selected rows a run's budget covers.

Per-row cost is the worst measured per-finished-row call count, refute-close and revive
paths included, taken from two lessons-run receipts (one terminal commit call reserved off
the top in each, adjudicate calls counted as row cost):
- claude-klabauter 20261113T030828Z: 33 calls (triage 4, refute-close 5, fix 3, verify 19,
  adjudicate 1, commit 1); 16 rows resolved; 32 / 16 = 2.00 per row.
- example-stats-repo 20261113T035948Z: 44 calls (triage 4, refute-close 4, fix 7, verify 27,
  adjudicate 1, commit 1); halted budget-exhausted with 4 refuted-discard rows unfinished;
  the 12 rows that resolved cost 43 / 12 = 3.58 per row (the 16-row mean, 2.69, is a floor
  because the unfinished rows are in the denominator).
ceil(3.583) = 3.59 centi-rounded up gives 359, admitting 10 rows under max_agent_calls=40.
"""

from __future__ import annotations

from typing import NamedTuple, Optional, Sequence

ROW_AGENT_CALLS_CENTI = 359
RUN_FIXED_AGENT_CALLS = 1
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
) -> Admission:
    """Admit the longest run-order prefix both bounds cover; a None bound is unbounded.

    Raises ValueError on a non-positive bound rather than admitting zero rows silently.
    """
    for name, value in (("max_agent_calls", max_agent_calls), ("budget_tokens", budget_tokens)):
        if value is not None and value <= 0:
            raise ValueError(f"{name} must be positive, got {value}")
    rows = tuple(row_ids_in_run_order)
    by_calls = (
        None
        if max_agent_calls is None
        else max(0, ((max_agent_calls - RUN_FIXED_AGENT_CALLS) * 100) // ROW_AGENT_CALLS_CENTI)
    )
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
            "row_output_tokens": ROW_OUTPUT_TOKENS,
            "bound_by_calls": by_calls,
            "bound_by_tokens": by_tokens,
            "admitted_count": count,
        },
    )


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
