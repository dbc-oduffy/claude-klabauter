"""`AckStore` -- the accept-process record that a MUTATING dispatch was asked
before it ran, so `warm.request_status` can answer a poll without reading
side effects.

Implements `coordinator_core/contract/dispatch-ack-reconcile-contract.md`
(D1-D5). This module imports only the stdlib -- it is loaded by both the
accept process and, indirectly, by tests that construct a store in
isolation.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Optional

#: Fixed, generous capacity (contract § 7) -- not derived from the
#: op-latency sink's dispatch rate. Each record is tens of bytes; 4000
#: entries covers 10x the longest mutation read deadline (300s) at a
#: conservatively estimated 10 mutating dispatches/second.
DEFAULT_CAPACITY = 4000

_ADMITTED = "admitted"
_STAMPED = "stamped"
_TOMBSTONED = "tombstoned"

STATE_NOT_RECEIVED = "not_received"
STATE_EXECUTING = "executing"
STATE_FINISHED = "finished"
STATE_UNKNOWABLE = "unknowable"

REASON_ENGINE_RESTARTED = "engine-restarted"
REASON_EXPIRED = "expired"
REASON_NO_RESIDENT_ENGINE = "no-resident-engine"

#: The five stamped outcomes (contract § 3) -- never more.
OUTCOME_RESULT = "result"
OUTCOME_ERROR = "error"
OUTCOME_ABANDONED = "abandoned"
OUTCOME_WORKER_LOST = "worker-lost"
OUTCOME_NOT_DISPATCHED = "not-dispatched"

_FINISHED_OUTCOMES = (OUTCOME_RESULT, OUTCOME_ERROR)
_UNKNOWABLE_OUTCOMES = (OUTCOME_ABANDONED, OUTCOME_WORKER_LOST)


def mint_ns_of(key: str) -> Optional[int]:
    """The mint-time component of a `<pid>-<clock>_ns` dispatch key
    (contract § 1). `None` for a malformed key -- callers treat that as
    "cannot be compared against boot/low-water", never as a crash; an
    unparsable key still gets a store record via `admit`/`status`, just
    without the boot/low-water fast paths.
    """
    tail = key.rpartition("-")[2]
    try:
        return int(tail)
    except (ValueError, TypeError):
        return None


class AckStore:
    """One instance per resident engine process, guarded by one
    `threading.Lock` (contract § 2). `boot_ns` is stamped at construction --
    this engine's own birth instant, on the same clock the client mints
    dispatch keys on (C1's spike verdict). `status`'s tombstone insert for
    an absent, non-evicted key happens in the SAME critical section as its
    own lookup (D3) -- that is what closes the pipe-buffer race the
    2026-09-01 double-execution incident (row `077d1a9a38b1`) found.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY, *, boot_ns: Optional[int] = None) -> None:
        self._capacity = capacity
        self.boot_ns = time.monotonic_ns() if boot_ns is None else boot_ns
        #: Mint time of the oldest key ever evicted. Starts at `boot_ns`:
        #: nothing has been evicted yet, and a key minted before boot is
        #: already refused by the `boot_ns` comparison on its own.
        self.low_water_ns = self.boot_ns
        self._lock = threading.Lock()
        self._order: "deque[str]" = deque()
        self._records: dict = {}

    def admit(self, key: str, method: Optional[str]) -> bool:
        """"May this frame be dispatched, and is it now recorded as asked?"
        (contract § 2). Refuses a key already present in the store in any
        state -- admitted, stamped, or tombstoned -- and a key at or below
        `low_water_ns` or minted before `boot_ns`, even when no tombstone
        record for it personally survived eviction (D3, D4). This is what
        makes the `not-dispatched` re-run instruction sound: a replayed
        frame can never be admitted twice.
        """
        mint_ns = mint_ns_of(key)
        with self._lock:
            if key in self._records:
                return False
            if mint_ns is not None and (mint_ns < self.boot_ns or mint_ns <= self.low_water_ns):
                return False
            self._records[key] = {
                "state": _ADMITTED,
                "method": method,
                "outcome": None,
                "error_code": None,
            }
            self._order.append(key)
            self._evict_if_needed()
            return True

    def stamp(self, key: str, outcome: str, *, error_code: Optional[int] = None) -> None:
        """"Did the handler return, and how?" (contract § 3). A no-op for a
        key this store has no admitted record of (evicted, or never
        admitted) -- stamping is best-effort and never raises on the
        caller's behalf; the poll simply answers per whatever state the key
        is actually in.
        """
        with self._lock:
            record = self._records.get(key)
            if record is None or record["state"] != _ADMITTED:
                return
            record["state"] = _STAMPED
            record["outcome"] = outcome
            record["error_code"] = error_code

    def status(self, key: str) -> dict:
        """Answer the poll for `key` -- exactly one of the four states
        (contract § 4), always. Never itself refuses (§ 5): a poll is
        answered directly from this store, outside `admit`'s tombstone/skew
        refusal path.
        """
        mint_ns = mint_ns_of(key)
        with self._lock:
            if mint_ns is not None and mint_ns < self.boot_ns:
                return {"state": STATE_UNKNOWABLE, "reason": REASON_ENGINE_RESTARTED}
            if mint_ns is not None and mint_ns <= self.low_water_ns and key not in self._records:
                return {"state": STATE_UNKNOWABLE, "reason": REASON_EXPIRED}

            record = self._records.get(key)
            if record is None:
                # D3: tombstone NOW, in this same critical section, so a
                # frame carrying this key arriving after this answer can
                # never be admitted -- turning "never read (yet)" into
                # "never read and never will".
                self._records[key] = {
                    "state": _TOMBSTONED,
                    "method": None,
                    "outcome": None,
                    "error_code": None,
                }
                self._order.append(key)
                self._evict_if_needed()
                return {"state": STATE_NOT_RECEIVED, "outcome": OUTCOME_NOT_DISPATCHED}

            if record["state"] == _TOMBSTONED:
                return {"state": STATE_NOT_RECEIVED, "outcome": OUTCOME_NOT_DISPATCHED}
            if record["state"] == _ADMITTED:
                return {"state": STATE_EXECUTING}

            outcome = record["outcome"]
            if outcome == OUTCOME_NOT_DISPATCHED:
                return {"state": STATE_NOT_RECEIVED, "outcome": OUTCOME_NOT_DISPATCHED}
            if outcome in _FINISHED_OUTCOMES:
                payload = {"state": STATE_FINISHED, "outcome": outcome}
                if outcome == OUTCOME_ERROR and record.get("error_code") is not None:
                    payload["error_code"] = record["error_code"]
                return payload
            if outcome in _UNKNOWABLE_OUTCOMES:
                # No named reason in § 6 covers a stamped abandoned/worker-lost
                # outcome; the outcome value itself is the honest reason a
                # caller gets no re-run license here.
                return {"state": STATE_UNKNOWABLE, "reason": outcome}
            # Unreachable in practice (every `stamp` call passes one of the
            # five named outcomes); fail closed to unknowable rather than
            # raise out of a poll.
            return {"state": STATE_UNKNOWABLE, "reason": REASON_NO_RESIDENT_ENGINE}

    def _evict_if_needed(self) -> None:
        """FIFO eviction over the one store, raising `low_water_ns` to the
        evicted key's mint time (§ 5) -- called under `self._lock` only.
        """
        while len(self._order) > self._capacity:
            evicted_key = self._order.popleft()
            self._records.pop(evicted_key, None)
            mint_ns = mint_ns_of(evicted_key)
            if mint_ns is not None and mint_ns > self.low_water_ns:
                self.low_water_ns = mint_ns
