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

OUTCOME_RESULT = "result"
OUTCOME_ERROR = "error"
OUTCOME_ABANDONED = "abandoned"
OUTCOME_WORKER_LOST = "worker-lost"
OUTCOME_NOT_DISPATCHED = "not-dispatched"

_FINISHED_OUTCOMES = (OUTCOME_RESULT, OUTCOME_ERROR)
_UNKNOWABLE_OUTCOMES = (OUTCOME_ABANDONED, OUTCOME_WORKER_LOST)


def mint_ns_of(key: str) -> Optional[int]:
    tail = key.rpartition("-")[2]
    try:
        return int(tail)
    except (ValueError, TypeError):
        return None


class AckStore:

    def __init__(self, capacity: int = DEFAULT_CAPACITY, *, boot_ns: Optional[int] = None) -> None:
        self._capacity = capacity
        self.boot_ns = time.monotonic_ns() if boot_ns is None else boot_ns
        self.low_water_ns = self.boot_ns
        self._lock = threading.Lock()
        self._order: "deque[str]" = deque()
        self._records: dict = {}

    def admit(self, key: str, method: Optional[str]) -> bool:
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
        with self._lock:
            record = self._records.get(key)
            if record is None or record["state"] != _ADMITTED:
                return
            record["state"] = _STAMPED
            record["outcome"] = outcome
            record["error_code"] = error_code

    def status(self, key: str) -> dict:
        mint_ns = mint_ns_of(key)
        with self._lock:
            if mint_ns is not None and mint_ns < self.boot_ns:
                return {"state": STATE_UNKNOWABLE, "reason": REASON_ENGINE_RESTARTED}
            if mint_ns is not None and mint_ns <= self.low_water_ns and key not in self._records:
                return {"state": STATE_UNKNOWABLE, "reason": REASON_EXPIRED}

            record = self._records.get(key)
            if record is None:
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
                # caller gets no re-run license here.
                return {"state": STATE_UNKNOWABLE, "reason": outcome}
            return {"state": STATE_UNKNOWABLE, "reason": REASON_NO_RESIDENT_ENGINE}

    def _evict_if_needed(self) -> None:
        while len(self._order) > self._capacity:
            evicted_key = self._order.popleft()
            self._records.pop(evicted_key, None)
            mint_ns = mint_ns_of(evicted_key)
            if mint_ns is not None and mint_ns > self.low_water_ns:
                self.low_water_ns = mint_ns
