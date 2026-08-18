"""coordinator_core.warm.telemetry — warm lifecycle observability.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C26

WHY THIS IS LOAD-BEARING, not decoration. With warmth opt-in (C23) and
demand-driven (idle demotion, C24), "was this session served warm?" stops
being a property of the box and becomes a per-session fact -- an
unobservable one by default. DR-313 item 5 makes this concrete against a
live precedent: DoE's live-tree env override is silent and inherited
through `child_env()`, so exempt sessions are unidentifiable, and that DR
downgrades it from "mitigation" to "an exemption that must be made
observable before any coverage claim is credible." A warm engine with an
opt-in and an idle timer has exactly that shape and must not repeat it.
With no `warm.status` verb, this module is the SOLE observability
surface by design -- "was this session warm?" has no other answer.

WHAT THIS MODULE RECORDS, cheaply, on an already-open in-process
structure (`ServerTelemetry`, one instance per server life -- no new
spawn, no new file per invocation):

  1. Whether each invocation was served warm or cold
     (`record_invocation(warm=...)`).
  2. The reason a server exited -- `EXIT_REASON_SKEW` (C16's
     `evict_on_skew`), `EXIT_REASON_IDLE_DEMOTION` (C24's
     `demote_if_idle`), `EXIT_REASON_OPERATOR_STOP` (the operator stop
     hatch, `coordinator/bin/warm-engine-stop.py`), or
     `EXIT_REASON_DEGRADED` (a self-detected degraded-health stop) --
     recorded by `record_exit(reason)`.
  3. The served-invocation count per server life (`served_count()`) --
     not decoration: it is the direct measurement of the amortization
     argument this plan rests on (a server serving ~130 invocations per
     2.1-minute life), and C21's re-measurement cannot honestly assert
     warmth paid off without it. `served_count` is also the exact
     zero-arg shape C24's `idle.ServedCountFn` seam expects, so a caller
     wires `telemetry.served_count` straight into `idle.should_demote` /
     `idle.demote_if_idle`'s `served_count=` argument with no adapter.

WIRING NOTE (scope boundary, not a gap in this module): this row's
`writes:` is exactly `{telemetry.py, this module's own test file}`.
Constructing a `ServerTelemetry` at server boot, calling
`record_invocation` from the dispatch seam, calling `record_exit` from
each of the four trigger call sites, and calling `flush()` from C17's
`ctx_shutdown` step are all edits to files OUTSIDE that list
(`warm/server.py`, `warm/skew.py`, `warm/idle.py`,
`coordinator/bin/warm-engine-stop.py`) and are therefore NOT made by
this chunk -- this module supplies the recording primitives and the
on-disk flush target; wiring each call site is a follow-up chunk's job,
the same scope split `warm.breadcrumb`'s own docstring already
documents for its own storage/decision-vs-wiring split.

ON-DISK SHAPE: `state/warm/telemetry.jsonl`, one JSON line appended per
`flush()` -- an APPEND log, not a latest-snapshot file like
`warm.breadcrumb`'s `warm.json`, because the thing worth reading back is
a HISTORY of server lives (C27's soak explicitly reads "served-invocation
count per server life" across many short lives), not only the most
recent one. `svc_dir()` reuses the exact `<engine_root>/state/warm/`
resolution `warm.breadcrumb.svc_dir` already establishes as this
package's precedent for a resident-engine concern's on-disk home, rather
than inventing a second directory convention.

NEGATIVE-SPEC:
  - Does NOT decide WHEN a server exits or WHY -- that is each trigger's
    own job (`warm.skew.evict_on_skew`, `warm.idle.demote_if_idle`, the
    operator stop hatch, a degraded self-stop); this module only records
    the reason a caller reports.
  - Does NOT call `warm.lifecycle.begin_shutdown` / `drain_and_exit`, and
    is not itself part of the single-shot shutdown guard -- flushing
    telemetry is orthogonal to, and safely re-orderable around, the
    four-step shutdown sequence (though C17's own step-3 language ---
    "flush the log" -- names this module's `flush()` as that step's
    eventual body once wired).
  - Does NOT raise past a flush failure -- a telemetry write must never
    be the reason `ctx_shutdown()` (and therefore the whole shutdown
    sequence, since step 3 precedes `os._exit` in `warm.lifecycle`) fails
    to complete.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from coordinator_core import locked_write
from coordinator_core.warm.breadcrumb import svc_dir

__all__ = [
    "EXIT_REASON_SKEW",
    "EXIT_REASON_IDLE_DEMOTION",
    "EXIT_REASON_OPERATOR_STOP",
    "EXIT_REASON_DEGRADED",
    "EXIT_REASONS",
    "TELEMETRY_FILENAME",
    "telemetry_path",
    "ServerTelemetry",
]

EXIT_REASON_SKEW = "skew"
EXIT_REASON_IDLE_DEMOTION = "idle-demotion"
EXIT_REASON_OPERATOR_STOP = "operator-stop"
EXIT_REASON_DEGRADED = "degraded"

EXIT_REASONS = frozenset(
    {
        EXIT_REASON_SKEW,
        EXIT_REASON_IDLE_DEMOTION,
        EXIT_REASON_OPERATOR_STOP,
        EXIT_REASON_DEGRADED,
    }
)

TELEMETRY_FILENAME = "telemetry.jsonl"


def telemetry_path(engine_root: Optional[Path] = None) -> Path:
    """`<svc dir>/telemetry.jsonl` for `engine_root` -- same `state/warm/`
    directory `warm.breadcrumb.svc_dir` resolves, see module docstring's
    "ON-DISK SHAPE"."""
    return svc_dir(engine_root) / TELEMETRY_FILENAME


class ServerTelemetry:
    """One instance per server life. Thread-safe counters on an
    already-open structure -- every connection thread in `warm.server`
    may call `record_invocation` concurrently, so all mutation is behind
    a single lock, mirroring `warm.server.InFlightCounter`'s own shape.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self._started_monotonic = clock()
        self._served_count = 0
        self._warm_count = 0
        self._cold_count = 0
        self._exit_reason: Optional[str] = None

    def record_invocation(self, *, warm: bool) -> int:
        """Record one served invocation as warm or cold. Returns the
        running served-invocation count (post-increment) -- the module
        docstring's point 3, and the exact value `served_count()` (below)
        also returns, kept in sync under the same lock.
        """
        with self._lock:
            self._served_count += 1
            if warm:
                self._warm_count += 1
            else:
                self._cold_count += 1
            return self._served_count

    def served_count(self) -> int:
        """Zero-arg served-invocation count -- the exact shape C24's
        `idle.ServedCountFn` expects, so this method binds directly into
        `idle.should_demote(served_count=telemetry.served_count, ...)`
        with no adapter (module docstring's "WHAT THIS MODULE RECORDS",
        point 3).
        """
        with self._lock:
            return self._served_count

    def record_exit(self, reason: str) -> None:
        """Record why this server is exiting. `reason` must be one of
        `EXIT_REASONS` (skew / idle-demotion / operator-stop / degraded).
        First call wins -- a server exits at most once (`warm.lifecycle`'s
        single-shot guard), so a second call is a caller bug, not a
        legitimate second exit; it is silently ignored rather than raised,
        since telemetry recording must never be the reason a shutdown
        sequence fails (module docstring's negative-spec).
        """
        if reason not in EXIT_REASONS:
            raise ValueError(f"unknown exit reason: {reason!r}, expected one of {sorted(EXIT_REASONS)}")
        with self._lock:
            if self._exit_reason is None:
                self._exit_reason = reason

    def snapshot(self) -> dict:
        """A point-in-time dict of this server life's counters -- the
        record shape `flush()` appends, also useful directly in tests
        without touching disk.
        """
        with self._lock:
            return {
                "served_count": self._served_count,
                "warm_count": self._warm_count,
                "cold_count": self._cold_count,
                "exit_reason": self._exit_reason,
                "life_seconds": self._clock() - self._started_monotonic,
            }

    def flush(self, *, engine_root: Optional[Path] = None) -> None:
        """Append this server life's `snapshot()` (plus a wall-clock
        `flushed_at`) as one JSON line to `telemetry_path()`. Best-effort:
        never raises past a lock timeout or `OSError`, mirroring
        `warm.breadcrumb.unlink_breadcrumb`'s "never raises" contract --
        this is meant to be called from C17's `ctx_shutdown` step, which
        must complete before `os._exit(0)` regardless of whether the
        telemetry write itself succeeded (module docstring's
        negative-spec).
        """
        record = self.snapshot()
        record["flushed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        path = telemetry_path(engine_root)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with locked_write.held_lock(path, holder_label="warm.telemetry"):
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            return
