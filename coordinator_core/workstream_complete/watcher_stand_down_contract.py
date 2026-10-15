"""Shared contract for the workstream-complete watcher stand-down op: name and rows.

Leaf module, types and constants only. The op, the registry and the tests agree on this vocabulary
without importing each other. The list a StopRow sits in is its outcome, so there is no per-row
outcome enum.

The result key `expires_on_its_own` lists one-shot subscriptions (notify_when_idle) that fire or
expire with no cancel verb: informational, no agent action and not a stop step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

OP_NAME = "workstream_complete.stand_down_watchers"

ATTRIBUTION_LAUNCH_RECORD = "launch-record"
ATTRIBUTION_ANCHOR_DESCENT = "anchor-descent"


@dataclass(frozen=True)
class StopRow:
    """One watcher tree stopped. `attribution` is the key that tied it to the session; `killed`
    holds (pid, ctime) pairs; `task_id` is None for an anchor-descent match."""

    watcher_id: str
    attribution: str
    killed: Tuple[Tuple[int, int], ...] = field(default_factory=tuple)
    task_id: Optional[str] = None


@dataclass(frozen=True)
class StandDown:
    """Outcome of the Group EM nomination stand-down; `exit_code` follows the nomination CLI
    (0 stood down, 3 no record, 4 lock failure, 5 not the holder)."""

    ok: bool
    exit_code: int
    message: str


__all__ = [
    "OP_NAME",
    "ATTRIBUTION_LAUNCH_RECORD",
    "ATTRIBUTION_ANCHOR_DESCENT",
    "StopRow",
    "StandDown",
]
