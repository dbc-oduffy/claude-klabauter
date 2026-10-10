"""Shared contract for guard-heavy-command-admission: types, protocols, key names, constants.

Leaf module, stdlib only. Every heavy-admission helper imports its vocabulary from here so the
classifier, identity, probe, lease store, census and guard agree on names without importing each
other.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Sequence, runtime_checkable

GUARD_NAME = "guard-heavy-command-admission"

LEG_IDENTITY = "identity"
LEG_RAM_FLOOR = "ram-floor"
LEG_SESSION_CAP = "session-cap"
LEG_BOX_HOLD = "box-hold"
LEGS = (LEG_IDENTITY, LEG_BOX_HOLD, LEG_RAM_FLOOR, LEG_SESSION_CAP)
# Advisory record kind in the would-deny log: never a leg, never a deny.
ADVISORY_WORKER_RSS = "worker-rss"

OVERRIDE_KEYS = {
    LEG_IDENTITY: "COORDINATOR_ALLOW_HEAVY_IDENTITY",
    LEG_BOX_HOLD: "COORDINATOR_ALLOW_HEAVY_BOX_HOLD",
    LEG_RAM_FLOOR: "COORDINATOR_ALLOW_HEAVY_RAM_FLOOR",
    LEG_SESSION_CAP: "COORDINATOR_ALLOW_HEAVY_SESSION_CAP",
}

KEY_FREE_RAM_FLOOR_MB = "heavy_admission.free_ram_floor_mb"
KEY_SESSION_HEAVY_CAP = "heavy_admission.session_heavy_cap"
KEY_SESSION_BACKGROUND_CAP = "heavy_admission.session_background_cap"
KEY_LEASE_RESERVE_MB = "heavy_admission.lease_reserve_mb"
# A vitest run at or under this many workers (flag or repo config) is not heavy.
KEY_VITEST_MAX_WORKERS = "heavy_admission.vitest_max_workers"
# A session's test worker over this working set is flagged in the would-deny log; never denies.
KEY_WORKER_RSS_CEILING_MB = "heavy_admission.worker_rss_ceiling_mb"
MACHINE_LOCAL_KEYS = (
    KEY_FREE_RAM_FLOOR_MB,
    KEY_SESSION_HEAVY_CAP,
    KEY_SESSION_BACKGROUND_CAP,
    KEY_LEASE_RESERVE_MB,
    KEY_VITEST_MAX_WORKERS,
    KEY_WORKER_RSS_CEILING_MB,
)

ALLOWLIST_PATH = Path(__file__).resolve().parent / "heavy_command_allowlist.txt"

# A lease still unattributed to a heavy descendant after this long is reaped.
LEASE_ATTRIBUTION_TTL_S = 120

# An orphaned process tree is claimed by a lease only if its root was created within this many
# seconds after the lease's launch mark.
ORPHAN_CLAIM_WINDOW_S = 30

# The image stems a lease of each class may claim; a tree holding none of them is not its command.
CLASS_IMAGES = {
    "typecheck": frozenset({"node", "tsc"}),
    "build": frozenset({"node", "next", "vite", "cargo", "rustc", "dotnet", "pnpm", "npm"}),
    "test_tier": frozenset({"node", "vitest", "pytest", "python", "python3"}),
    "ue": frozenset({"unrealeditor", "unrealeditor-cmd", "unrealbuildtool", "ubt", "runuat", "dotnet"}),
    "reindex": frozenset({"python", "python3", "node"}),
}


class HeavyClass(str, enum.Enum):
    TYPECHECK = "typecheck"
    BUILD = "build"
    TEST_TIER = "test_tier"
    UE = "ue"
    REINDEX = "reindex"


@dataclass(frozen=True)
class Classification:
    """heavy_class is None for a command that is not heavy; scoped marks the RAM-leg-exempt
    explicit-test-target carve-out; background mirrors tool_input.run_in_background; noemit_tsc
    marks a command whose every heavy segment is a one-shot `tsc --noEmit` (the executor carve-out);
    bounded_verify marks one whose heavy segments are all test-tier or typecheck runs that exit on
    their own, with vitest/jest workers capped at or under the box cap (the verifier carve-out)."""

    heavy_class: Optional[HeavyClass]
    scoped: bool
    background: bool
    noemit_tsc: bool = False
    bounded_verify: bool = False


@dataclass(frozen=True)
class MemoryReading:
    """avail_mb is None when unreadable; trusted False means the reading must deny heavy launches."""

    avail_mb: Optional[int]
    trusted: bool
    source: str


@dataclass(frozen=True)
class ProcRow:
    pid: int
    ppid: int
    ctime: int
    name: str


@dataclass(frozen=True)
class LeaseRecord:
    """A (pid, creation_time) admission lease; the holder starts as the session anchor and is
    narrowed by the next census. launch_ctime is the hook caller's creation time, in the host's
    native ctime units, so an orphaned command tree can be matched to it; 0 means unknown."""

    holder_pid: int
    holder_ctime: int
    session_pid: int
    session_ctime: int
    heavy_class: str
    admitted_at: float
    launch_ctime: int = 0


@runtime_checkable
class ProcessPrimitives(Protocol):
    """The spawn-free process seam; lease store and census take it as a parameter so tests stub it."""

    def alive(self, pid: int, ctime: int) -> bool:
        """True only when pid is running and its creation time equals ctime."""
        ...

    def creation_time(self, pid: int) -> Optional[int]: ...

    def snapshot(self) -> Optional[Sequence[ProcRow]]:
        """None when the process table is unreadable."""
        ...
