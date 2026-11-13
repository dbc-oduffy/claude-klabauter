"""
coordinator_core.distill.autofire_contract -- shared contract for distill autofire.

Fixes the constants, the decision record and the launcher protocol that
`coordinator_core.distill.autofire.on_idle_tick` (decision + exactly-once guard)
and `coordinator_core.distill.autofire_launch.launch` (the launcher) implement,
so the warm idle tick, the decision logic and the launcher agree on one shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from coordinator_core.ops import check_harvest_debt

# Fire-record tag (`expected=` on the fired workflow) marking a distill autofire.
AUTOFIRE_EXPECTED = "distill-autofire"

# Single source of truth for the threshold: the harvest-debt nudge threshold.
AUTOFIRE_THRESHOLD: int = check_harvest_debt._NUDGE_THRESHOLD

# Minimum seconds between autofire evaluations per repo; below
# warm.idle.DEFAULT_IDLE_MINUTES (15 min) so the tick is never the bottleneck.
AUTOFIRE_INTERVAL_SECS = 600


@dataclass(frozen=True)
class AutofireDecision:
    """Outcome of one autofire evaluation for `repo_root`.

    `fire` is True only when the launcher should run; `reason` is a short
    machine-stable token naming why; `debt` is the unharvested-spec count.
    """

    repo_root: Path
    fire: bool
    reason: str
    debt: int


class Launcher(Protocol):
    """Starts a distill run for `repo_root` and returns the fire record (dict)."""

    def __call__(self, repo_root: Path) -> dict: ...


# Entry points C1-C3 implement; callers import them by these dotted names.
ON_IDLE_TICK_ENTRY = "coordinator_core.distill.autofire.on_idle_tick"
LAUNCH_ENTRY = "coordinator_core.distill.autofire_launch.launch"

__all__ = [
    "AUTOFIRE_EXPECTED",
    "AUTOFIRE_THRESHOLD",
    "AUTOFIRE_INTERVAL_SECS",
    "AutofireDecision",
    "Launcher",
    "ON_IDLE_TICK_ENTRY",
    "LAUNCH_ENTRY",
]
