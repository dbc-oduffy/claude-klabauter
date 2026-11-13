"""Which finished chunks of a halted run passed review.

Invariant: a "ledger-check" halt is raised by the verify leg, which runs after the review phase
(`ask_contract.ASK_PHASES` ends in "review"; `ASK_VERIFY_LABEL` replaced the "ledger-check" leg).
So when the halt kind is ledger-check and the scoped tests passed, every finished chunk was
reviewed, except rows the halt recorded as stopped.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from coordinator_core.ops.dispatch_emit import ask_contract

_PASSING_CHECKS = frozenset({"pass", "pass-on-retest"})


def review_passed_chunks(
    halted: Mapping[str, Any], test_check: str, finished: Iterable[str]
) -> frozenset[str]:
    """Return the finished chunk ids whose product a halt commit may land.

    Empty unless `halted["halted"]` is the ledger-check kind and `test_check` is "pass" or
    "pass-on-retest"; ids in `halted["stopped_rows"]` are excluded. Pure: no I/O.
    """
    if halted.get("halted") != ask_contract.LEDGER_HALT_KIND:
        return frozenset()
    if test_check not in _PASSING_CHECKS:
        return frozenset()
    stopped = halted.get("stopped_rows") or ()
    return frozenset(finished) - frozenset(stopped)
