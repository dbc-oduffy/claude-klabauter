"""Shared contract for the TaskStop orphan-tree reaper: record, outcomes, kill protocol, names.

Leaf module, types and constants only. The recorder, reaper, launch store, attribution and kill
primitive agree on this vocabulary without importing each other. The attribution window pair is
deliberately absent: it is a measured constant owned by the attribution module.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional, Protocol, Tuple, runtime_checkable

from coordinator_core.bash_guards._heavy_admission_contract import ProcRow

RECORDER_OP = "hooks.taskstop_launch_recorder"
REAPER_OP = "hooks.taskstop_reaper"

# Must exceed the life of any background task (a `tsc --watch` lives until session exit).
RECORD_TTL_S = 7 * 24 * 3600

# Relative to settings_home().
STORE_RELPATH = "taskstop-reaper/launches"
LOG_RELPATH = "taskstop-reaper/reaper.jsonl"


@dataclass(frozen=True)
class LaunchRecord:
    """One background launch. `mark` is a FILETIME int taken at recorder entry and is also the
    RECORD_TTL_S age basis."""

    task_id: str
    session_id: str
    tool_use_id: str
    command: str
    mark: int


class ReapOutcome(enum.Enum):
    KILLED = "killed"
    INCOMPLETE = "incomplete"
    AMBIGUOUS = "ambiguous"
    NO_RECORD = "no-record"
    SESSION_MISMATCH = "session-mismatch"
    NO_CANDIDATE = "no-candidate"


@dataclass(frozen=True)
class ReapRow:
    """One reaper-log row. `killed` holds (pid, ctime) pairs; `competing` the task ids whose
    launch windows overlapped; `candidates` the orphan trees that matched."""

    task_id: str
    outcome: ReapOutcome
    killed: Tuple[Tuple[int, int], ...] = field(default_factory=tuple)
    competing: Tuple[str, ...] = field(default_factory=tuple)
    candidates: int = 0


@runtime_checkable
class KillPrimitives(Protocol):
    """Kill seam. terminate_verified returns True only for a (pid, ctime)-verified process
    confirmed dead; a mismatch, denial or gone pid is False and never raises. command_line is None
    on any failure."""

    def terminate_verified(self, pid: int, ctime: int) -> bool: ...

    def command_line(self, pid: int, ctime: int) -> Optional[str]: ...


__all__ = [
    "RECORDER_OP",
    "REAPER_OP",
    "RECORD_TTL_S",
    "STORE_RELPATH",
    "LOG_RELPATH",
    "LaunchRecord",
    "ReapOutcome",
    "ReapRow",
    "KillPrimitives",
    "ProcRow",
]
