"""Registry of lints that tolerate existing hits, each with a pinned census that only falls."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class LintCensus:
    """A lint's live existing-hit counter, its pinned count, and the ledger the count reads."""

    name: str
    count: Callable[[], int]
    pin: int
    ledger: str


def _home_resolution_count() -> int:
    from coordinator_core.tests import _home_resolution_lint_baseline as ledger

    return (
        len(ledger.X_OK_BASELINE)
        + len(ledger.COLON_JOIN_BASELINE)
        + len(ledger.FORWARD_SLASH_BASELINE)
        + len(ledger.BARE_OR_BASELINE)
    )


REGISTRY: dict[str, LintCensus] = {
    "home-resolution-lint": LintCensus(
        name="home-resolution-lint",
        count=_home_resolution_count,
        pin=10,
        ledger="coordinator_core/tests/_home_resolution_lint_baseline.py",
    ),
}


def check(name: str) -> list[str]:
    """Problems for one registered lint: live above pin (regrew) or below it (stale pin)."""
    entry = REGISTRY[name]
    live = entry.count()
    if live > entry.pin:
        return [
            f"{name}: live count {live} exceeds pin {entry.pin} (regrew); "
            f"fix the new hits, do not add rows to {entry.ledger}"
        ]
    if live < entry.pin:
        return [f"{name}: live count {live} is below pin {entry.pin}; lower the pin to {live}"]
    return []
