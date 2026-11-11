"""Pure grind admission arithmetic: how many selected rows a run's budget covers.

Per-row cost constants come from the 15 run-cost records plus run 20261006T105900Z's
memo, whose itemised stage tally over 14 attempted rows is triage 5, fix 15, verify 13,
commit 11. The per-row commit call is gone, leaving 33 calls over 14 rows, 2.357 per row,
rounded up to 2.36. The one terminal commit is reserved off the top as a fixed call.
"""

from __future__ import annotations

from typing import NamedTuple, Optional, Sequence

ROW_AGENT_CALLS_CENTI = 236
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
