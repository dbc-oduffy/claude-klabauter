"""Pure grind admission arithmetic: how many selected rows a run's budget covers.

Per-row cost constants come from the 15 run-cost records plus run 20261006T105900Z's
memo, whose itemised 44-call stage tally over 14 attempted rows is 3.143 agent calls
per row, rounded up to 3.15.
"""

from __future__ import annotations

from typing import NamedTuple, Optional, Sequence

ROW_AGENT_CALLS_CENTI = 315
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
    by_calls = None if max_agent_calls is None else (max_agent_calls * 100) // ROW_AGENT_CALLS_CENTI
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
