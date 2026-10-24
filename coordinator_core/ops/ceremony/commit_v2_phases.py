"""Per-dispatch phase telemetry for ceremony.commit_v2: thread-CPU per step, one ledger row.

Time is thread CPU plus the dispatch's charged-child CPU bucket, never wall clock. `emit` never
raises. The op_latency import is lazy so commit_v2's import closure grows only by this module.
"""
from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path
from typing import ContextManager, Optional

OP = "ceremony.commit_v2"


def _child_cpu_secs() -> float:
    # sys.modules lookup: importing ipc here would grow commit_v2's import set.
    ipc = sys.modules.get("coordinator_core.ipc")
    bucket = getattr(getattr(ipc, "_op_cpu_secs_var", None), "get", lambda: None)()
    return float(sum(bucket)) if bucket else 0.0


def _dispatch_op() -> Optional[str]:
    ipc = sys.modules.get("coordinator_core.ipc")
    accessor = getattr(ipc, "current_dispatch_op", None)
    return accessor() if accessor else None


class PhaseLedger:
    """Accumulates CPU milliseconds per named phase; `emit` writes one `phase_time` row."""

    def __init__(self, corr_id: Optional[str] = None) -> None:
        self.corr_id = corr_id
        self._ms: dict[str, float] = {}

    @contextlib.contextmanager
    def _bracket(self, name: str):
        t0 = time.thread_time()
        c0 = _child_cpu_secs()
        try:
            yield
        finally:
            delta = (time.thread_time() - t0) + (_child_cpu_secs() - c0)
            self._ms[name] = self._ms.get(name, 0.0) + delta * 1000.0

    def phase(self, name: str) -> ContextManager[None]:
        """Bracket a step; re-entry of the same name sums."""
        return self._bracket(name)

    def phases_ms(self) -> dict[str, float]:
        """Phase name -> CPU ms, in first-seen order."""
        return dict(self._ms)

    def emit(
        self,
        repo_root: Optional[Path],
        *,
        abandoned: bool,
        paths: int,
        spawns: Optional[int],
    ) -> None:
        """Write one `phase_time` row (counts only, no path strings); never raises.

        `dispatch_op` is the method of the dispatch the handler ran inside: a value other than
        `ceremony.commit_v2` marks a nested call sharing that dispatch's corr_id.
        """
        try:
            from coordinator_core import dispatch_abandon
            from coordinator_core.telemetry import op_latency

            phases = self.phases_ms()
            op_latency._write_entry(
                {
                    "kind": "phase_time",
                    "op": OP,
                    "t_start": time.time(),
                    "corr_id": self.corr_id,
                    "dispatch_op": _dispatch_op(),
                    "phases_ms": phases,
                    "total_ms": sum(phases.values()),
                    "abandoned": bool(abandoned or dispatch_abandon.is_abandoned()),
                    "paths": int(paths),
                    "spawns": spawns,
                },
                repo_root,
            )
        except Exception:
            pass
